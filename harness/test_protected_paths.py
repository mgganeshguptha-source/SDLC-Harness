"""Files no phase may write never reach the working tree, the WIP commit or the
PR, whatever the build did to them (OP018-6418 Story 2, 2026-11-11)."""
import subprocess
from pathlib import Path

import pytest

import protected_paths as PP


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "service"
    (r / ".harness").mkdir(parents=True)
    (r / ".harness" / "config.yaml").write_text(
        'target_module: "mpa-web"\nwrite_exclude:\n  - "**/package-lock.json"\n')
    (r / "mpa-web" / "node_modules" / "mpa-js").mkdir(parents=True)
    (r / "mpa-web" / "node_modules" / "mpa-js" / "a.js").write_text("a")
    (r / "mpa-web" / "node_modules" / "mpa-js" / "b.js").write_text("b")
    (r / "mpa-web" / "package-lock.json").write_text("{}")
    (r / "mpa-web" / "src" / "app").mkdir(parents=True)
    (r / "mpa-web" / "src" / "app" / "x.ts").write_text("old")
    _git(r, "init", "-q", "-b", "master")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "mpa-web")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return r


def _npm_ci_and_code_change(r):
    # what npm ci does to a committed node_modules, plus a real code change
    for f in (r / "mpa-web" / "node_modules" / "mpa-js").iterdir():
        f.unlink()
    (r / "mpa-web" / "node_modules" / "other").mkdir()
    (r / "mpa-web" / "node_modules" / "other" / "i.js").write_text("installed")
    (r / "mpa-web" / "package-lock.json").write_text('{"changed": 1}')
    (r / "mpa-web" / "src" / "app" / "x.ts").write_text("new")


def test_restore_worktree_puts_back_tracked_files_only(repo):
    _npm_ci_and_code_change(repo)
    restored = PP.restore_worktree(repo)
    assert (repo / "mpa-web" / "node_modules" / "mpa-js" / "a.js").read_text() == "a"
    assert (repo / "mpa-web" / "package-lock.json").read_text() == "{}"
    assert (repo / "mpa-web" / "src" / "app" / "x.ts").read_text() == "new"
    assert len(restored) == 3


def test_unstage_keeps_only_the_code_change(repo):
    _npm_ci_and_code_change(repo)
    _git(repo, "add", "-A")
    out = PP.unstage(repo)
    staged = _git(repo, "diff", "--cached", "--name-only").split()
    assert [s for s in staged if s.startswith("mpa-web/")] == ["mpa-web/src/app/x.ts"]
    assert "mpa-web/node_modules/other/i.js" in out
    assert (repo / "mpa-web" / "node_modules" / "mpa-js" / "b.js").is_file()
    assert (repo / "mpa-web" / "node_modules" / "other" / "i.js").is_file()   # kept on disk


def test_filter_drops_protected_paths_from_pr_lists(repo, tmp_path):
    changed, deleted = tmp_path / "changed", tmp_path / "deleted"
    changed.write_bytes(b"mpa-web/src/app/x.ts\0mpa-web/package-lock.json\0")
    deleted.write_bytes(b"mpa-web/node_modules/mpa-js/a.js\0mpa-web/src/app/gone.ts\0")
    dropped = PP.filter_lists(repo, [changed, deleted])
    assert changed.read_bytes() == b"mpa-web/src/app/x.ts\0"
    assert deleted.read_bytes() == b"mpa-web/src/app/gone.ts\0"
    assert sorted(dropped) == ["mpa-web/node_modules/mpa-js/a.js", "mpa-web/package-lock.json"]


def test_api_spec_in_generated_module_stays_editable(tmp_path):
    r = tmp_path / "svc"
    (r / ".harness").mkdir(parents=True)
    (r / ".harness" / "config.yaml").write_text(
        'generated_module: ["svc-openapi-code"]\n'
        'api_spec_files: ["svc-openapi-code/src/main/resources/svc.yml"]\n')
    globs = PP.protected_globs(r)
    assert not PP.is_protected("svc-openapi-code/src/main/resources/svc.yml", globs)
    assert PP.is_protected("svc-openapi-code/src/main/java/Gen.java", globs)


def test_cli_filter(repo, tmp_path, capsys):
    changed, deleted = tmp_path / "c", tmp_path / "d"
    changed.write_bytes(b"")
    deleted.write_bytes(b"mpa-web/node_modules/mpa-js/a.js\0")
    assert PP.main(["filter", "--repo", str(repo), "--changed", str(changed),
                    "--deleted", str(deleted)]) == 0
    assert "left out of the PR: 1 path(s)" in capsys.readouterr().out


@pytest.mark.parametrize("path,pattern,expected", [
    ("mpa-web/node_modules/mpa-js/a.js", "**/node_modules/**", True),
    ("node_modules/x/a.js", "**/node_modules/**", True),
    ("mpa-web/src/app/node_modules_note.ts", "**/node_modules/**", False),
    ("src/main/java/A.java", "src/main/**", True),
    ("a/b/generated/X.java", "**/generated/**", True),
])
def test_wildcard_folder_globs(path, pattern, expected):
    from boundaries import _matches
    assert _matches(path, pattern) is expected
