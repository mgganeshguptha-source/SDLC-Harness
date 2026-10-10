"""
test_learning.py — the clarification-churn fixes, with no SDK and no network.

  - the context gate reads only a file written THIS attempt, and a phase that
    wrote none halts as ARTIFACT_MISSING, never as a story question
  - a resume at context re-reads the story from the base branch
  - repo-wide instructions and approved decision files reach the prompt
  - the report counts questions asked again, in the same story or another

Run:  python -m pytest test_learning.py -q
"""
import os
import subprocess
import tempfile
import time
from pathlib import Path

import halt_gates as HG
from clarification import scan_context
from harness_report import clarification_stats
from phases import PHASES, phase_by_id
from resume import refresh_story, rewind
from state import RunState
from story_source import read_story_from_ref, story_hash
from test_state_machine import ScriptedExecutor, _hd, _new_run, _sm


# ---- context gate: only this attempt's file counts -------------------------

def _old_context(repo: Path, text: str) -> Path:
    d = repo / ".github" / "story-context-files"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "old-story-context.md"
    f.write_text(text, encoding="utf-8")
    past = time.time() - 3600
    os.utime(f, (past, past))
    return f


def test_scan_ignores_files_older_than_the_attempt():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _old_context(repo, "**VERDICT: GO**\nNo [NEEDS CLARIFICATION] items.\n")
        cr = scan_context(repo, written_after=time.time() - 1)
        assert cr.missing and not cr.clear
        # Without the cut-off the old file is still found (other callers).
        assert not scan_context(repo).missing


class _WritesNothing(ScriptedExecutor):
    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        return self.script.get(phase.id, 0)


def test_missing_context_file_is_a_technical_halt_not_a_question():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        # A clean context file from an EARLIER story is lying in the folder.
        _old_context(hd.parent, "**VERDICT: GO**\nNone.\n")
        sm = _sm(_WritesNothing(), hd)
        run = sm.run_until_pause(_new_run())
        assert run.status == "halted", run.status
        assert run.halt_gate == HG.ARTIFACT_MISSING
        assert run.current_phase == "context"
        assert "context" not in run.completed_phases


def test_real_clarification_still_needs_input():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor(context_clarifications=True), hd)
        run = sm.run_until_pause(_new_run())
        assert run.status == "needs_input"
        assert run.halt_gate == HG.CLARIFICATION


# ---- resume re-reads the story from the base branch ------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo_with_story(d: str, name: str, text: str) -> Path:
    repo = Path(d)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "stories").mkdir()
    (repo / "stories" / name).write_text(text, encoding="utf-8")
    (repo / "stories" / "current-story.md").write_text("shared story", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "story")
    return repo


def _commit_story(repo: Path, name: str, text: str):
    (repo / "stories" / name).write_text(text, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "answer")


def test_read_story_from_ref_finds_the_feature_file_case_insensitively():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_story(d, "op018-6418-STORY.md", "the story\n")
        text, path = read_story_from_ref(repo, "main", "OP018-6418",
                                         "stories/current-story.md")
        assert text == "the story"
        assert path == "stories/op018-6418-STORY.md"
        # No per-story file: falls back to the configured file.
        text, path = read_story_from_ref(repo, "main", "OTHER-1",
                                         "stories/current-story.md")
        assert (text, path) == ("shared story", "stories/current-story.md")


def _halted_run(story: str, at: str) -> RunState:
    run = RunState(feature_id="OP018-6418", story=story, current_phase=at,
                   status="needs_input")
    run.story_sha256 = story_hash(story)
    return run


def test_resume_at_context_uses_the_answered_story():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_story(d, "OP018-6418-story.md", "old story")
        _commit_story(repo, "OP018-6418-story.md", "old story\nAC-9: answered")
        run = rewind(_halted_run("old story", "context"), "context", log=lambda *a: None)
        refresh_story(run, repo, "main", "stories/current-story.md", log=lambda *a: None)
        assert run.story.endswith("AC-9: answered")
        assert run.story_sha256 == story_hash(run.story)
        assert run.story_path == "stories/OP018-6418-story.md"


def test_resume_at_context_warns_when_story_unchanged():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_story(d, "OP018-6418-story.md", "same story")
        logs = []
        run = rewind(_halted_run("same story", "context"), "context", log=lambda *a: None)
        refresh_story(run, repo, "main", "stories/current-story.md", log=logs.append)
        assert any("UNCHANGED" in m for m in logs)


def test_resume_later_keeps_the_original_story():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_story(d, "OP018-6418-story.md", "edited later")
        logs = []
        run = rewind(_halted_run("original", "code_review"), "code_review",
                     log=lambda *a: None)
        refresh_story(run, repo, "main", "stories/current-story.md", log=logs.append)
        assert run.story == "original"
        assert any("keeps the ORIGINAL story" in m for m in logs)


def test_unreadable_base_never_fails_the_resume():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo_with_story(d, "OP018-6418-story.md", "x")
        run = rewind(_halted_run("original", "context"), "context", log=lambda *a: None)
        refresh_story(run, repo, "no-such-branch", "stories/current-story.md",
                      log=lambda *a: None)
        assert run.story == "original"


# ---- knowledge reaches the prompt -------------------------------------------

def _service(d: str, config: str = "") -> Path:
    repo = Path(d)
    (repo / ".harness").mkdir()
    (repo / ".harness" / "config.yaml").write_text(config, encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github" / "copilot-instructions.md").write_text(
        "Use constructor injection.", encoding="utf-8")
    dec = repo / "docs" / "decisions"
    dec.mkdir(parents=True)
    (dec / "OP018-6418.md").write_text(
        "# Decisions — OP018-6418\n## D-OP018-6418-1: unknown status maps to UNKNOWN\n",
        encoding="utf-8")
    return repo


def test_decisions_and_repo_instructions_reach_the_context_prompt():
    from sdk_runner import _load_capability_layer
    with tempfile.TemporaryDirectory() as d:
        repo = _service(d)
        text, manifest = _load_capability_layer(repo, phase_by_id("context"))
        assert "Use constructor injection." in text
        assert "D-OP018-6418-1" in text
        assert [k["path"] for k in manifest["knowledge"]] == ["docs/decisions/OP018-6418.md"]
        assert any(i.get("repo_wide") for i in manifest["instructions"])


def test_decisions_stay_out_of_phases_not_listed():
    from sdk_runner import _load_capability_layer
    with tempfile.TemporaryDirectory() as d:
        repo = _service(d)
        text, manifest = _load_capability_layer(repo, phase_by_id("coding"))
        assert "D-OP018-6418-1" not in text
        assert manifest["knowledge"] == []
        assert "Use constructor injection." in text   # repo-wide: every phase


def test_knowledge_cap_drops_files_over_budget():
    from sdk_runner import _load_capability_layer
    with tempfile.TemporaryDirectory() as d:
        repo = _service(d, "knowledge_max_chars: 10\n")
        text, manifest = _load_capability_layer(repo, phase_by_id("context"))
        assert "D-OP018-6418-1" not in text
        assert manifest["knowledge"][0].get("skipped") == "knowledge_max_chars"


def test_documentation_prompt_asks_for_decisions_and_proposals():
    from sdk_runner import _phase_instruction
    with tempfile.TemporaryDirectory() as d:
        repo = _service(d)
        run = RunState(feature_id="OP018-6418", story="s", current_phase="documentation")
        run.story_path, run.story_sha256 = "stories/OP018-6418-story.md", "ab" * 32
        prompt = _phase_instruction(phase_by_id("documentation"), run, repo)
        assert "docs/decisions/OP018-6418.md" in prompt
        assert "toolkit-proposal/PROPOSAL.md" in prompt
        assert "stories/OP018-6418-story.md @ abababababab" in prompt


# ---- the report: is it learning? --------------------------------------------

def _rec(fid, t, gate=None, items=(), h=None, repo="org/svc", status="needs_input"):
    return {"feature_id": fid, "repo": repo, "started_at": t, "halt_gate": gate,
            "clarification_items": list(items), "story_sha256": h, "status": status}


def test_report_counts_repeats_in_same_and_other_stories():
    q = "[NEEDS CLARIFICATION]: What should happen for an unrecognised status value?"
    recs = [
        _rec("A-1", "2026-10-01", "clarification", [q], "h1"),
        _rec("A-1", "2026-10-02", "clarification", [q], "h1"),      # unanswered, repeat
        _rec("A-1", "2026-10-03", status="done", h="h2"),
        _rec("B-2", "2026-10-04", "clarification",
             ["[NEEDS CLARIFICATION]: what should happen for an unrecognized status value"],
             "h9"),                                                   # other story
        _rec("C-3", "2026-10-05", "clarification", [q], "h5", repo="org/other"),
    ]
    st = clarification_stats(recs)
    assert st["halts"] == 4
    assert st["repeat_same"] == 1
    assert st["repeat_other"] == 1          # B-2 repeats A-1; C-3 is another repo
    assert st["features"]["A-1"]["unanswered"] == 1
    assert st["features"]["A-1"]["done"] is True
