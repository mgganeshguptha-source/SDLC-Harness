"""
gitignore.py — make sure the service repo's .gitignore keeps harness working
files out of the branches people merge.

WHY
Setting up a repo means copying .harness/config.yaml and the caller workflow,
and adding four .gitignore lines. The last step is the one people forget, and
the harness then works but its workspace (.harness/), audit trail and metrics
can be committed by a developer by accident — or, worse, `.harness/` is
ignored as a whole and config.yaml can never be committed.

WHAT IT DOES (fresh runs, when manage_gitignore is on — the default)
  - adds any missing required line to the repo's .gitignore, in a marked block:
        .harness/*
        !.harness/config.yaml
        audit/
        metrics/
  - fixes the wrong form `.harness/` (or `.harness`), which stops Git from ever
    re-including config.yaml: it is replaced by the two lines above;
  - keeps the file's own line endings (CRLF stays CRLF) and every other line.
The change is made in the run's working tree, rides on the WIP branch, and
ships in the harness pull request for the developer to review like any other
change. A repo that already has the lines is not touched.

It never adds entries for files a team may commit (.github/skills,
.github/instructions, stories/, attachments).
"""
from __future__ import annotations

from pathlib import Path

REQUIRED = [".harness/*", "!.harness/config.yaml", "audit/", "metrics/"]
BLOCK_START = "# --- SDLC-Harness: harness working files (added by the harness) ---"
BLOCK_END = "# --- end SDLC-Harness ---"
# A line that ignores the whole .harness folder: Git cannot re-include a file
# inside an excluded directory, so config.yaml could never be committed.
_WHOLE_DIR_KEY = ".harness"


def _key(line: str) -> str:
    """Compare .gitignore lines by meaning: '/audit/', 'audit/' and 'audit' all
    ignore the root audit folder; '!/.harness/config.yaml' = '!.harness/config.yaml'."""
    s = line.strip()
    neg = s.startswith("!")
    body = (s[1:] if neg else s).strip().lstrip("/").rstrip("/")
    return ("!" if neg else "") + body


def _is_whole_dir(line: str) -> bool:
    return _key(line) == _WHOLE_DIR_KEY


def plan(text: str) -> tuple[list[str], list[str]]:
    """(lines to add, whole-folder .harness lines to replace) for this text."""
    lines = text.splitlines()
    present = {_key(l) for l in lines if l.strip() and not l.strip().startswith("#")}
    wrong = [l for l in lines if _is_whole_dir(l)]
    missing = [r for r in REQUIRED if _key(r) not in present]
    return missing, wrong


def ensure(repo_root: Path, log=print) -> list[str]:
    """Update <repo>/.gitignore. Returns the lines added ([] = unchanged).
    Never raises: a .gitignore problem must not stop a run."""
    p = Path(repo_root) / ".gitignore"
    try:
        raw = p.read_bytes() if p.exists() else b""
        nl = "\r\n" if b"\r\n" in raw else "\n"
        text = raw.decode("utf-8", errors="replace")
        missing, wrong = plan(text)
        if not missing and not wrong:
            log("  [gitignore] .gitignore already has the harness entries — unchanged")
            return []

        lines = text.splitlines()
        if wrong:
            lines = [l for l in lines if not _is_whole_dir(l)]
            missing, _ = plan(nl.join(lines))
            log(f"  [gitignore] replaced {', '.join(repr(w.strip()) for w in wrong)} — "
                f"it ignores the whole folder, so .harness/config.yaml could never be "
                f"committed")

        while lines and not lines[-1].strip():
            lines.pop()
        block = ([""] if lines else []) + [BLOCK_START] + missing + [BLOCK_END]
        out = nl.join(lines + block) + nl
        p.write_bytes(out.encode("utf-8"))
        log(f"  [gitignore] added to .gitignore: {', '.join(missing)} "
            f"(ships in the harness PR for review)")
        return missing
    except Exception as e:
        log(f"  [gitignore] could not update .gitignore ({type(e).__name__}: {e}) — "
            f"add these lines by hand: {', '.join(REQUIRED)}")
        return []


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Add the harness entries to .gitignore")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--check", action="store_true",
                    help="report only; exit 1 when entries are missing")
    a = ap.parse_args()
    if a.check:
        gi = Path(a.repo) / ".gitignore"
        miss, bad = plan(gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else "")
        print("missing:", miss or "none", "| wrong form:", bad or "none")
        raise SystemExit(1 if (miss or bad) else 0)
    ensure(Path(a.repo))
