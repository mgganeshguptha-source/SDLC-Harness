"""
test_analysis_mode.py — ANALYSIS MODE (bug-fix analysis), with NO SDK.

Proves, with a scripted executor and temp folders:
  - the mode picks its own short phase sequence and leaves build mode untouched;
  - the analysis phase may write the analysis document and nothing else;
  - a clean document is stamped with the analysed source, copied to the story
    folder and given a PR body, and the run ends without any build phase;
  - an open [NEEDS CLARIFICATION] line halts the run and publishes nothing;
  - resume, config fallbacks, the task prompt and `init --mode`.

Run:  python test_analysis_mode.py
 or:  python -m pytest test_analysis_mode.py -v
"""
import argparse
import tempfile
from pathlib import Path

import analysis as AN
import halt_gates as HG
from boundaries import is_write_allowed
from config import HarnessConfig
from contracts import ExitCode
from phases import (ALL_PHASES, ANALYSIS_DOC, ANALYSIS_PHASES, PHASES, first_phase,
                    next_phase, normalize_mode, phase_by_id, phases_for)
from state import RunState
from state_machine import StateMachine

STORY = ("Title: Owner full name drops the last name\n"
         "**Requires a new or changed contract from:** None\n"
         "Steps to reproduce: GET /owners/1 -> fullName is 'George'.\n")

CLEAN_DOC = ("# Bug analysis — PC-9\n\n"
             "## 1. Symptom\nfullName omits the last name.\n\n"
             "## 5. Root cause\nOwner.getFullName() returns firstName only — "
             "src/main/java/Owner.java:42\n\n"
             "## 11. Open questions and assumptions\nNone.\n")

OPEN_DOC = ("# Bug analysis — PC-9\n\n"
            "## 5. Root cause\nNot established.\n\n"
            "## 11. Open questions and assumptions\n"
            "- [NEEDS CLARIFICATION]: the stack trace or log lines for the failing request\n")


class ScriptedExecutor:
    """Returns OK and writes the analysis document the test asks for."""

    def __init__(self, doc=CLEAN_DOC, code=ExitCode.OK):
        self.doc = doc
        self.code = code
        self.calls = []
        self.repo_root = None

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        if self.code == ExitCode.OK and self.doc is not None:
            p = self.repo_root / ANALYSIS_DOC
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.doc, encoding="utf-8")
        return self.code


def _repo(d):
    hd = Path(d) / ".harness"
    hd.mkdir(parents=True, exist_ok=True)
    return Path(d), hd


def _sm(ex, hd):
    ex.repo_root = hd.parent
    return StateMachine(ex, hd, repo_root=hd.parent, log=lambda *a: None)


def _run():
    return RunState(feature_id="PC-9", story=STORY,
                    current_phase=first_phase("analysis").id, mode="analysis")


def _drive(sm, run, max_steps=20):
    for _ in range(max_steps):
        run = sm.run_until_pause(run)
        if run.status == "awaiting_approval":
            run = sm.resolve_gate(run, approved=True)
        elif run.status in ("done", "halted", "needs_input"):
            break
    return run


# ---------------------------------------------------------------- sequences
def test_modes_have_separate_sequences():
    assert [p.id for p in phases_for("analysis")] == ["bug_analysis"]
    assert phases_for("build") is PHASES
    assert phases_for(None) is PHASES and phases_for("") is PHASES
    assert first_phase("analysis").id == "bug_analysis"
    assert first_phase().id == PHASES[0].id


def test_unknown_mode_falls_back_to_build():
    assert normalize_mode("ANALYSIS ") == "analysis"
    assert normalize_mode("nonsense") == "build"
    assert normalize_mode(None) == "build"


def test_next_phase_follows_the_mode():
    assert next_phase("bug_analysis", "analysis") is None
    # Build mode is unchanged, with or without the argument.
    assert next_phase("coding").id == "code_review"
    assert next_phase("coding", "build").id == "code_review"
    assert next_phase(PHASES[-1].id) is None


def test_phase_ids_unique_across_modes():
    ids = [p.id for p in ALL_PHASES]
    assert len(ids) == len(set(ids))
    assert phase_by_id("bug_analysis") is ANALYSIS_PHASES[0]
    assert phase_by_id("coding").id == "coding"


def test_old_run_state_without_mode_is_a_build_run():
    with tempfile.TemporaryDirectory() as d:
        _, hd = _repo(d)
        RunState(feature_id="PC-1", story="s", current_phase="context").save(hd)
        import json
        f = hd / "run-state.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("mode"); data.pop("analysis_doc")
        f.write_text(json.dumps(data), encoding="utf-8")
        run = RunState.load(hd)
        assert run.mode == "build" and run.analysis_doc is None


# ----------------------------------------------------------- write boundary
def test_analysis_phase_may_write_only_the_document():
    ph = phase_by_id("bug_analysis")
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        cfg = HarnessConfig.load(hd, repo_root=repo)
        allowed, exclude = cfg.phase_writes(ph), cfg.phase_exclude(ph)
        ok = lambda p: is_write_allowed(p, allowed, repo_root=str(repo),  # noqa: E731
                                        exclude_globs=exclude)
        assert ok(".harness/bug-analysis.md")
        assert ok(str(repo / ".harness" / "bug-analysis.md"))
        for denied in ("src/main/java/Owner.java", "src/test/java/OwnerTest.java",
                       "src/app/owner.component.ts", "pom.xml", "package.json",
                       "docs/x.md", "stories/PC-9/PC-9.md",
                       "stories/PC-9/PC-9-bug-analysis.md",
                       ".github/copilot-instructions.md", ".harness/config.yaml",
                       ".harness/prompt-steps.md"):
            assert not ok(denied), denied


def test_analysis_phase_is_read_only_in_a_multi_module_repo():
    ph = phase_by_id("bug_analysis")
    cfg = HarnessConfig(target_module="svc-application")
    allowed = cfg.phase_writes(ph)
    assert tuple(allowed) == (ANALYSIS_DOC,)
    assert not is_write_allowed("svc-application/src/main/java/A.java", allowed)


# ------------------------------------------------------------ the happy path
def test_clean_analysis_is_published_and_the_run_ends():
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        ex = ScriptedExecutor()
        run = _drive(_sm(ex, hd), _run())
        assert run.status == "done", (run.status, run.halt_detail)
        assert ex.calls == ["bug_analysis"]          # no build phase ever ran
        assert run.completed_phases == ["bug_analysis"]
        assert run.analysis_doc == "stories/PC-9/PC-9-bug-analysis.md"
        published = (repo / run.analysis_doc).read_text(encoding="utf-8")
        assert published.startswith("# Bug analysis — PC-9\n")
        assert AN.STAMP_BEGIN in published and "Commit:" in published
        assert "Owner.java:42" in published
        # The workspace copy matches what was published (it goes to the audit folder).
        assert (repo / ANALYSIS_DOC).read_text(encoding="utf-8") == published
        body = (hd / "pr-body.md").read_text(encoding="utf-8")
        assert run.analysis_doc in body and "No code was changed" in body
        assert "mode: build" in body


def test_published_path_follows_attachments_dir():
    cfg = HarnessConfig(attachments_dir="work/{feature_id}/inputs")
    assert AN.doc_rel(cfg, "OP018-1") == "work/OP018-1/inputs/OP018-1-bug-analysis.md"
    assert AN.doc_rel(HarnessConfig(), "PC-9") == "stories/PC-9/PC-9-bug-analysis.md"


def test_stamp_is_replaced_not_duplicated():
    src = {"commit": "abc", "branch": "b", "analysed_at": "t", "story_path": "s",
           "story_sha256": "h", "run_id": "1", "actor": "a"}
    once = AN.stamped(CLEAN_DOC, src)
    twice = AN.stamped(once, dict(src, commit="def"))
    assert twice.count(AN.STAMP_BEGIN) == 1
    assert "`def`" in twice and "`abc`" not in twice
    assert twice.startswith("# Bug analysis — PC-9\n")
    # A document with no heading still gets the block, at the top.
    assert AN.stamped("no heading here\n", src).startswith(AN.STAMP_BEGIN)


# ------------------------------------------------------------------ the gate
def test_open_question_halts_and_publishes_nothing():
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        ex = ScriptedExecutor(doc=OPEN_DOC)
        run = _drive(_sm(ex, hd), _run())
        assert run.status == "needs_input"
        assert run.halt_gate == HG.CLARIFICATION
        assert run.analysis_doc is None
        assert "bug_analysis" not in run.completed_phases
        assert not (repo / "stories").exists()
        assert not (hd / "pr-body.md").exists()
        assert run.clarification_log and "stack trace" in run.clarification_log[-1]["questions"][0]
        # The draft stays in the workspace for the audit folder.
        assert (repo / ANALYSIS_DOC).is_file()


def test_scan_ignores_none_lines_and_prose_mentions():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.md"
        f.write_text("## Open questions\nNone.\nNo [NEEDS CLARIFICATION] items.\n",
                     encoding="utf-8")
        assert AN.scan(f) == []
        f.write_text("- [NEEDS CLARIFICATION]: which environment\n"
                     "* [NEEDS CLARIFICATION]: the request payload\n", encoding="utf-8")
        assert len(AN.scan(f)) == 2
        assert AN.scan(Path(d) / "missing.md") == []


def test_failed_phase_halts_without_publishing():
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        ex = ScriptedExecutor(doc=None, code=ExitCode.ARTIFACT_MISSING)
        run = _drive(_sm(ex, hd), _run())
        assert run.status == "halted" and run.halt_gate == HG.ARTIFACT_MISSING
        assert run.analysis_doc is None and not (repo / "stories").exists()


def test_boundary_violation_halts_the_analysis_run():
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        ex = ScriptedExecutor(doc=None, code=ExitCode.BOUNDARY_VIOLATION)
        run = _drive(_sm(ex, hd), _run())
        assert run.status == "halted" and run.halt_gate == HG.WRITE_BOUNDARY


# ---------------------------------------------------------------- build mode
def test_build_run_never_enters_the_analysis_phase():
    class Rec:
        calls = []
        repo_root = None

        def run_phase(self, phase, run):
            self.calls.append(phase.id)
            return ExitCode.SDK_ERROR      # stop at the first executed phase

    with tempfile.TemporaryDirectory() as d:
        _, hd = _repo(d)
        ex = Rec()
        run = RunState(feature_id="PC-1", story=STORY, current_phase=PHASES[0].id)
        run = _drive(_sm(ex, hd), run)
        assert run.mode == "build"
        assert "bug_analysis" not in ex.calls and ex.calls


# -------------------------------------------------------------------- resume
def test_resume_rewinds_an_analysis_run():
    import resume
    run = _run()
    run.status = "needs_input"
    run.iterations = {"bug_analysis": 12, "__runs__:bug_analysis": 3}
    run.last_feedback = "old"
    resume.rewind(run, "bug_analysis", log=lambda *a: None)
    assert run.current_phase == "bug_analysis" and run.status == "running"
    assert run.iterations == {"bug_analysis": 0, "__runs__:bug_analysis": 0}
    assert run.last_feedback is None and run.mode == "analysis"


def test_resume_rejects_a_build_phase_in_an_analysis_run():
    import resume
    try:
        resume.rewind(_run(), "coding", log=lambda *a: None)
    except SystemExit as e:
        assert "analysis" in str(e) and "bug_analysis" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_resume_of_a_build_run_is_unchanged():
    import resume
    run = RunState(feature_id="PC-1", story=STORY, current_phase="code_review",
                   completed_phases=["context", "prompt_steps", "coding"])
    resume.rewind(run, "coding", log=lambda *a: None)
    assert run.completed_phases == ["context", "prompt_steps"]


# -------------------------------------------------------------------- config
def test_analysis_skill_loads_even_when_a_repo_overrides_phase_skills():
    assert HarnessConfig().skills_for_phase("bug_analysis") == ["analyze-bug"]
    old = HarnessConfig(phase_skills={"context": ["build-context"]})
    assert old.skills_for_phase("bug_analysis") == ["analyze-bug"]
    own = HarnessConfig(phase_skills={"bug_analysis": ["my-analysis"]})
    assert own.skills_for_phase("bug_analysis") == ["my-analysis"]


def test_analysis_always_receives_attachments_and_a_file_scope():
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _repo(d)
        (hd / "config.yaml").write_text(
            "attachment_phases: [context]\n"
            "phase_file_scope:\n  context: ['src/app/**']\n", encoding="utf-8")
        cfg = HarnessConfig.load(hd, repo_root=repo)
        assert "bug_analysis" in cfg.attachment_phases
        assert cfg.attachment_phases[0] == "context"
        assert cfg.file_scope_for_phase("bug_analysis") == ["src/app/**"]
        assert cfg.file_scope_for_phase("raise_pr") == []


def test_analysis_model_is_set_per_phase():
    cfg = HarnessConfig(model="default-model",
                        phase_models={"bug_analysis": "strong-model"})
    assert cfg.model_for_phase("bug_analysis") == "strong-model"
    assert cfg.model_for_phase("coding") == "default-model"


# -------------------------------------------------------------------- prompt
def test_analysis_phase_has_a_task_prompt():
    import sdk_runner
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo(d)
        text = sdk_runner._phase_instruction(phase_by_id("bug_analysis"), _run(), repo)
        assert "analyze-bug" in text and "bug-analysis.md" in text
        assert "[NEEDS CLARIFICATION]" in text and "MUST NOT create or edit" in text
        assert "GET /owners/1" in text          # the bug report itself


def test_prompt_carries_earlier_questions_on_a_resume():
    import sdk_runner
    run = _run()
    run.clarification_log = [{"questions": ["[NEEDS CLARIFICATION]: the stack trace"]}]
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo(d)
        text = sdk_runner._phase_instruction(phase_by_id("bug_analysis"), run, repo)
        assert "QUESTIONS ASKED IN EARLIER ROUNDS" in text
        assert "Treat those answers as evidence" in text


# ---------------------------------------------------------------------- init
def _init_repo(d):
    repo, hd = _repo(d)
    (hd / "repo_list.yaml").write_text("repos: []\n", encoding="utf-8")
    (hd / "config.yaml").write_text("manage_gitignore: false\n", encoding="utf-8")
    (repo / "stories").mkdir()
    (repo / "stories" / "PC-9.md").write_text(STORY, encoding="utf-8")
    return repo, hd


def test_init_sets_the_mode_and_first_phase():
    import run as cli
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _init_repo(d)
        cli.cmd_init(argparse.Namespace(repo=str(repo), feature="PC-9", story=None,
                                        mode="analysis"))
        r = RunState.load(hd)
        assert r.mode == "analysis" and r.current_phase == "bug_analysis"
        cli.cmd_init(argparse.Namespace(repo=str(repo), feature="PC-9", story=None))
        r = RunState.load(hd)
        assert r.mode == "build" and r.current_phase == PHASES[0].id


def test_init_rejects_an_unknown_mode():
    import run as cli
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _init_repo(d)
        try:
            cli.cmd_init(argparse.Namespace(repo=str(repo), feature="PC-9",
                                            story=None, mode="analyse"))
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")
        assert RunState.load(hd) is None


def test_fake_runner_end_to_end():
    """The real executor (boundary + required-artifact checks) with the fake
    runner: no SDK, no credits."""
    from executor import PhaseExecutor
    from fake_runner import FakeAgentRunner
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _init_repo(d)
        ex = PhaseExecutor(FakeAgentRunner(log=lambda *a: None), repo_root=repo,
                           harness_dir=hd, log=lambda *a: None)
        sm = StateMachine(ex, hd, repo_root=repo, log=lambda *a: None)
        run = _drive(sm, _run())
        assert run.status == "done", (run.status, run.halt_gate, run.halt_detail)
        assert (repo / "stories" / "PC-9" / "PC-9-bug-analysis.md").is_file()


def test_fake_runner_out_of_bounds_write_is_caught():
    from executor import PhaseExecutor
    from fake_runner import FakeAgentRunner
    with tempfile.TemporaryDirectory() as d:
        repo, hd = _init_repo(d)
        ex = PhaseExecutor(FakeAgentRunner(misbehave_in="bug_analysis",
                                           log=lambda *a: None),
                           repo_root=repo, harness_dir=hd, log=lambda *a: None)
        sm = StateMachine(ex, hd, repo_root=repo, log=lambda *a: None)
        run = _drive(sm, _run())
        assert run.status == "halted" and run.halt_gate == HG.WRITE_BOUNDARY
        assert run.analysis_doc is None


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
