"""
protected_paths.py — keep files no phase may write out of the working tree
changes, the WIP commit and the PR.

THE PROBLEM (OP018-6418 Story 2, 2026-11-11)
dx_mpa_responsive commits a package (mpa-js) under mpa-web/node_modules/. The
build gate ran `npm ci`, which deletes node_modules/ before installing. The WIP
step staged the whole tree (`git add -A`), so the deletion was committed, and
the PR deleted 27 committed files. The write boundary only stops the AI: it
never covered what the BUILD changes.

THE RULE
A path is protected when it matches the repo's always-deny globs
(config write_exclude, which already includes generated modules), plus
`**/node_modules/**`. The OpenAPI file that coding may edit inside a generated
module stays editable (the same "!spec" re-include the coding phase uses).
No phase can legitimately change a protected path, so any change to one came
from a tool, and is undone:
  restore_worktree  after the build gate: tracked files put back from HEAD
  unstage           after `git add -A` on WIP: changes dropped from the commit
  filter            the PR file lists: protected paths removed

CLI (used by harness.yml):
  python protected_paths.py unstage --repo .
  python protected_paths.py filter  --repo . --changed FILE --deleted FILE
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ALWAYS = ["**/node_modules/**"]


def protected_globs(repo_root, harness_dir=None) -> list:
    from config import HarnessConfig
    repo_root = Path(repo_root)
    cfg = HarnessConfig.load(Path(harness_dir or repo_root / ".harness"), repo_root=repo_root)
    globs = [g for g in (cfg.write_exclude or []) if not str(g).startswith("!")]
    for g in ALWAYS:
        if g not in globs:
            globs.append(g)
    try:
        globs += [f"!{s}" for s in cfg._api_specs()]
    except Exception:
        pass
    return globs


def is_protected(path: str, globs) -> bool:
    from boundaries import is_excluded
    return is_excluded(path, globs)


def _git(repo_root, *args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True,
                          check=check)


def _batched(paths, n=200):
    for i in range(0, len(paths), n):
        yield paths[i:i + n]


def _split_z(raw: bytes) -> list:
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def restore_worktree(repo_root, harness_dir=None) -> list:
    """Put back tracked protected files the build changed or deleted. Untracked
    files are left alone (they are dealt with when committing)."""
    globs = protected_globs(repo_root, harness_dir)
    r = _git(repo_root, "status", "--porcelain=v1", "-z", "--no-renames",
             "--untracked-files=no")
    if r.returncode != 0:
        return []
    paths = []
    for entry in _split_z(r.stdout):
        path = entry[3:]
        if path and is_protected(path, globs):
            paths.append(path)
    for chunk in _batched(paths):
        _git(repo_root, "checkout", "HEAD", "--", *chunk)
    return paths


def unstage(repo_root, harness_dir=None) -> list:
    """After `git add -A`: drop staged changes to protected paths. A new file is
    removed from the index (kept on disk); a changed or deleted tracked file is
    put back from HEAD."""
    globs = protected_globs(repo_root, harness_dir)
    if _git(repo_root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return []
    r = _git(repo_root, "diff", "--cached", "--name-status", "-z", "--no-renames", "HEAD")
    if r.returncode != 0:
        return []
    items = _split_z(r.stdout)
    added, restore = [], []
    for status, path in zip(items[0::2], items[1::2]):
        if not is_protected(path, globs):
            continue
        (added if status.startswith("A") else restore).append(path)
    for chunk in _batched(added):
        _git(repo_root, "rm", "--cached", "-q", "--", *chunk)
    for chunk in _batched(restore):
        _git(repo_root, "checkout", "HEAD", "--", *chunk)
    return added + restore


def filter_lists(repo_root, list_files, harness_dir=None) -> list:
    """Remove protected paths from NUL-separated path lists, in place."""
    globs = protected_globs(repo_root, harness_dir)
    dropped = []
    for f in list_files:
        p = Path(f)
        if not p.is_file():
            continue
        paths = _split_z(p.read_bytes())
        keep = [x for x in paths if not is_protected(x, globs)]
        dropped += [x for x in paths if x not in keep]
        p.write_bytes(b"".join(x.encode("utf-8") + b"\0" for x in keep))
    return dropped


def _report(action: str, paths: list) -> None:
    if not paths:
        print(f"  [protected] {action}: none")
        return
    print(f"  [protected] {action}: {len(paths)} path(s) no phase may write, e.g.")
    for p in paths[:10]:
        print(f"      {p}")
    if len(paths) > 10:
        print(f"      ... and {len(paths) - 10} more")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("unstage")
    u.add_argument("--repo", default=".")
    f = sub.add_parser("filter")
    f.add_argument("--repo", default=".")
    f.add_argument("--changed", required=True)
    f.add_argument("--deleted", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "unstage":
        _report("left out of the WIP commit", unstage(a.repo))
    else:
        _report("left out of the PR", filter_lists(a.repo, [a.changed, a.deleted]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
