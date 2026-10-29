"""
test_gitignore.py — the harness's .gitignore entries (gitignore.py). No SDK.

Run:  python -m pytest test_gitignore.py -v
"""
import subprocess
from pathlib import Path

import gitignore as GI


def _quiet(*a, **k):
    pass


def test_creates_file_when_missing(tmp_path):
    added = GI.ensure(tmp_path, log=_quiet)
    assert added == GI.REQUIRED
    text = (tmp_path / ".gitignore").read_text()
    assert GI.BLOCK_START in text and text.endswith(GI.BLOCK_END + "\n")
    for r in GI.REQUIRED:
        assert r in text.splitlines()


def test_adds_only_missing_and_keeps_existing(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n/audit\n.harness/*\n\n", encoding="utf-8")
    added = GI.ensure(tmp_path, log=_quiet)
    assert added == ["!.harness/config.yaml", "metrics/"]
    lines = gi.read_text().splitlines()
    assert lines[:3] == ["node_modules/", "/audit", ".harness/*"]
    assert lines.count(".harness/*") == 1


def test_complete_file_is_untouched(tmp_path):
    gi = tmp_path / ".gitignore"
    original = b"target/\r\n/.harness/*\r\n!/.harness/config.yaml\r\naudit\r\nmetrics/\r\n"
    gi.write_bytes(original)
    assert GI.ensure(tmp_path, log=_quiet) == []
    assert gi.read_bytes() == original


def test_whole_folder_form_is_replaced(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("dist/\n.harness/\naudit/\nmetrics/\n", encoding="utf-8")
    added = GI.ensure(tmp_path, log=_quiet)
    assert added == [".harness/*", "!.harness/config.yaml"]
    lines = gi.read_text().splitlines()
    assert ".harness/" not in lines and "dist/" in lines


def test_crlf_is_preserved(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_bytes(b"target/\r\n*.log\r\n")
    GI.ensure(tmp_path, log=_quiet)
    raw = gi.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_git_really_ignores_workspace_but_keeps_config(tmp_path):
    """The point of the lines: with them, git ignores the harness workspace and
    the audit/metrics folders, yet .harness/config.yaml can be committed."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    GI.ensure(tmp_path, log=_quiet)
    (tmp_path / ".harness").mkdir()
    for rel in (".harness/config.yaml", ".harness/run-state.json",
                "audit/x/run.json", "metrics/2026/r.json", "src/app.ts"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")

    def ignored(rel):
        return subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", rel]).returncode == 0

    assert not ignored(".harness/config.yaml")
    assert ignored(".harness/run-state.json")
    assert ignored("audit/x/run.json") and ignored("metrics/2026/r.json")
    assert not ignored("src/app.ts")


def test_never_raises(tmp_path, monkeypatch):
    msgs = []
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("boom")))
    (tmp_path / ".gitignore").write_text("x\n")
    assert GI.ensure(tmp_path, log=msgs.append) == []
    assert "add these lines by hand" in msgs[-1]
