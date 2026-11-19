"""
test_baseline.py - the baseline build check (baseline.py) and the per-test
judgement it adds to the build gate (validation.run_validation).

The "build" here is a small script that behaves like Maven for the cases that
matter: it writes surefire reports, exits 1 on a failing test unless
-Dmaven.test.failure.ignore=true reached it through MAVEN_OPTS, exits 1 on a
compile error either way, and can report a missing library.

Run:  python -m pytest harness/test_baseline.py -v
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import baseline as BL
import halt_gates as HG
import validation
from state import RunState

FAKE_BUILD = r'''
import json, os, sys
from pathlib import Path
here = Path(sys.argv[1])
plan = json.loads((here / "build-plan.json").read_text())
count = here / "build-count.txt"
count.write_text(str(int(count.read_text() or 0) + 1) if count.exists() else "1")
(here / "maven-opts.txt").write_text(os.environ.get("MAVEN_OPTS", ""))
if plan.get("missing_library"):
    print("[ERROR] Failed to execute goal on project app: Could not resolve dependencies "
          "for project p:app:jar:1.0: Could not find artifact p:lib:jar:2.0 in store")
    print("[INFO] BUILD FAILURE"); sys.exit(1)
if plan.get("compile_error"):
    print("[ERROR] COMPILATION ERROR :")
    print("[ERROR] /w/app/src/main/java/p/A.java:[3,1] error: cannot find symbol")
    print("[INFO] BUILD FAILURE"); sys.exit(1)
rep = here / "app" / "target" / "surefire-reports"
rep.mkdir(parents=True, exist_ok=True)
failed = False
for cls, methods in plan.get("tests", {}).items():
    cases, bad = "", []
    for m, ok in methods.items():
        if ok:
            cases += f'<testcase name="{m}" classname="{cls}"/>'
        else:
            bad.append(m)
            cases += (f'<testcase name="{m}" classname="{cls}">'
                      f'<failure type="AssertionError">boom</failure></testcase>')
    (rep / f"TEST-{cls}.xml").write_text(
        f'<testsuite name="{cls}" tests="{len(methods)}" failures="{len(bad)}">{cases}</testsuite>')
    (rep / f"{cls}.txt").write_text(
        f"Test set: {cls}\nTests run: {len(methods)}, Failures: {len(bad)}, Errors: 0, Skipped: 0\n"
        + "".join(f"{cls}.{m} -- Time elapsed: 0.01 s <<< FAILURE!\n"
                  f"java.lang.AssertionError: boom in {cls}.{m}\n" for m in bad))
    if bad:
        failed = True
        print(f"[ERROR] Tests run: {len(methods)}, Failures: {len(bad)} <<< FAILURE! -- in {cls}")
        print("[ERROR] See target/surefire-reports for the individual test results.")
ignore = "-Dmaven.test.failure.ignore=true" in os.environ.get("MAVEN_OPTS", "")
if failed and not ignore:
    print("[INFO] BUILD FAILURE"); sys.exit(1)
print("[INFO] BUILD SUCCESS")
'''


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with one commit, a .harness/config.yaml and the fake build."""
    for v in ("HARNESS_RESUMED", "HARNESS_RUNS_BUILD", "GITHUB_WORKSPACE",
              "MAVEN_OPTS", "MAVEN_ARGS"):
        monkeypatch.delenv(v, raising=False)
    r = tmp_path / "service"
    hd = r / ".harness"
    hd.mkdir(parents=True)
    script = tmp_path / "fakebuild.py"
    script.write_text(FAKE_BUILD)
    (hd / "config.yaml").write_text(
        f'test_command: "{sys.executable} {script} {r}"\nmin_coverage: 0\n'
        'pre_validation_command: ""\n')
    (r / "README.md").write_text("base\n")
    (r / ".gitignore").write_text(".harness/*\n!.harness/config.yaml\nmetrics/\n"
                                  "app/\nbuild-*\nmaven-opts.txt\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.test")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def plan(repo, **kw):
    (repo / "build-plan.json").write_text(json.dumps(kw))


def builds(repo) -> int:
    f = repo / "build-count.txt"
    return int(f.read_text()) if f.exists() else 0


def new_run() -> RunState:
    return RunState(feature_id="OP-1", story="s", current_phase="context")


quiet = lambda *a, **k: None


# ------------------------------------------------------------------ helpers
def test_tolerant_env_adds_the_flag_once():
    e = BL.tolerant_env({"MAVEN_OPTS": "-Xmx1g"})
    assert e["MAVEN_OPTS"] == "-Xmx1g " + BL.IGNORE_FLAG
    assert e["MAVEN_ARGS"] == BL.IGNORE_FLAG
    assert BL.tolerant_env(e)["MAVEN_OPTS"].count(BL.IGNORE_FLAG) == 1


def test_results_are_read_per_test_and_old_reports_left_out(repo):
    import time
    plan(repo, tests={"p.ATest": {"ok": True, "bad": False}})
    validation.run_validation(repo, repo / ".harness", log=quiet, baseline=True)
    failing, seen = BL.read_test_results(repo)
    assert failing == ["p.ATest#bad"] and seen == 2
    assert BL.read_test_results(repo, since=time.time() + 60) == ([], 0)


def test_record_key_changes_with_base_store_and_command():
    k = BL.record_key("a" * 40, "b" * 40, "mvn test")
    assert k != BL.record_key("c" * 40, "b" * 40, "mvn test")
    assert k != BL.record_key("a" * 40, "d" * 40, "mvn test")
    assert k != BL.record_key("a" * 40, "b" * 40, "mvn verify")


# ----------------------------------------------------------- the build gate
def test_gate_is_unchanged_without_a_known_failing_list(repo):
    plan(repo, tests={"p.ATest": {"bad": False}})
    vr = validation.run_validation(repo, repo / ".harness", log=quiet)
    assert not vr.passed and vr.failure_kind == "test" and vr.exit_code == 1
    assert BL.IGNORE_FLAG not in (repo / "maven-opts.txt").read_text()


def test_gate_passes_when_only_already_failing_tests_fail(repo):
    plan(repo, tests={"p.ATest": {"ok": True, "bad": False}, "p.BTest": {"ok": True}})
    vr = validation.run_validation(repo, repo / ".harness", log=quiet,
                                   known_failing=["p.ATest#bad"])
    assert vr.passed and vr.tolerated_tests == ["p.ATest#bad"]
    assert "already failing" in vr.summary


def test_gate_fails_on_a_test_that_is_not_on_the_list(repo):
    plan(repo, tests={"p.ATest": {"bad": False}, "p.BTest": {"ok": True, "broke": False}})
    vr = validation.run_validation(repo, repo / ".harness", log=quiet,
                                   known_failing=["p.ATest#bad"])
    assert not vr.passed and vr.failure_kind == "test"
    assert "1 failing test(s) that do not fail on the base branch" in vr.summary
    # Only the new failure reaches the fixing phase, in the form routing reads.
    from state_machine import _failing_test_classes, _failing_test_methods
    assert _failing_test_classes(vr.output_tail) == ["BTest"]
    assert _failing_test_methods(vr.output_tail) == ["BTest.broke"]
    assert "boom in p.BTest.broke" in vr.output_tail
    assert "boom in p.ATest.bad" not in vr.output_tail
    assert validation.is_code_attributable(vr.output_tail)


def test_compile_error_still_fails_with_a_list(repo):
    plan(repo, compile_error=True)
    vr = validation.run_validation(repo, repo / ".harness", log=quiet,
                                   known_failing=["p.ATest#bad"])
    assert not vr.passed and vr.failure_kind == "test"
    assert "COMPILATION ERROR" in vr.output_tail


# --------------------------------------------------------- the baseline check
def test_clean_base_passes_and_is_built_once_per_base_commit(repo):
    plan(repo, tests={"p.ATest": {"ok": True}})
    run = new_run()
    assert BL.ensure(run, repo, repo / ".harness", log=quiet) is True
    assert run.baseline["status"] == "pass" and run.baseline["reused"] is False
    assert builds(repo) == 1
    saved = list((repo / "metrics" / "baseline").glob("*.json"))
    assert len(saved) == 1
    rec = json.loads(saved[0].read_text())
    # Not a run-metrics record: the dashboard must skip it.
    assert rec["record_type"] == "baseline" and "feature_id" not in rec

    # Another story on the same base commit: no second build.
    run2 = new_run()
    assert BL.ensure(run2, repo, repo / ".harness", log=quiet) is True
    assert run2.baseline["reused"] is True and builds(repo) == 1

    # The base branch moves: build again.
    (repo / "README.md").write_text("changed\n")
    _git(repo, "commit", "-q", "-am", "base moved")
    assert BL.ensure(new_run(), repo, repo / ".harness", log=quiet) is True
    assert builds(repo) == 2


def test_tests_that_already_fail_are_recorded_and_the_run_continues(repo):
    plan(repo, tests={"p.ATest": {"ok": True, "bad": False}})
    run = new_run()
    assert BL.ensure(run, repo, repo / ".harness", log=quiet) is True
    assert run.baseline["status"] == "tolerated"
    assert BL.known_failing(run) == ["p.ATest#bad"]
    assert run.status == "running"
    # The state survives a resume (it is in run-state.json).
    again = RunState.load(repo / ".harness")
    assert BL.known_failing(again) == ["p.ATest#bad"]
    # ... and the check is not repeated for the same run.
    assert BL.needed(again, repo / ".harness") == (False, "")


def test_compile_error_on_the_base_halts_before_any_phase(repo):
    plan(repo, compile_error=True)
    run = new_run()
    out = []
    assert BL.ensure(run, repo, repo / ".harness", log=out.append) is False
    assert run.status == "halted" and run.halt_gate == HG.BASELINE_BUILD
    assert HG.is_valid(run.halt_gate) and not HG.is_infra(run.halt_gate)
    text = "\n".join(out)
    assert "EXISTING BUILD FAILURE" in text and "FRESH run" in text
    assert "cannot find symbol" in text
    # Saved: the same base commit halts the next run without building again.
    run2 = new_run()
    assert BL.ensure(run2, repo, repo / ".harness", log=quiet) is False
    assert run2.halt_gate == HG.BASELINE_BUILD and builds(repo) == 1


def test_missing_library_halts_as_environment_and_is_not_saved(repo):
    plan(repo, missing_library=True)
    run = new_run()
    out = []
    assert BL.ensure(run, repo, repo / ".harness", log=out.append) is False
    assert run.status == "halted" and run.halt_gate == HG.CONFIG_ERROR
    assert "ENVIRONMENT FAILURE" in "\n".join(out)
    assert not (repo / "metrics" / "baseline").exists()
    # The library is added to the store: the next run builds again and passes.
    plan(repo, tests={"p.ATest": {"ok": True}})
    assert BL.ensure(new_run(), repo, repo / ".harness", log=quiet) is True
    assert builds(repo) == 2


def test_check_is_skipped_when_it_does_not_apply(repo, monkeypatch):
    hd = repo / ".harness"
    run = new_run()
    _cfg = (hd / "config.yaml").read_text()
    (hd / "config.yaml").write_text(_cfg + "\nruns_build: false\n")
    assert BL.needed(run, hd) == (False, "runs_build is false")
    assert BL.ensure(run, repo, hd, log=quiet) is True and builds(repo) == 0
    (hd / "config.yaml").write_text(_cfg)
    run.mode = "analysis"
    assert BL.needed(run, hd)[0] is False
    run.mode = "build"
    run.completed_phases = ["context"]          # an older run, already under way
    assert BL.needed(run, hd)[0] is False


def test_a_resume_does_not_use_or_write_the_saved_result(repo, monkeypatch):
    plan(repo, tests={"p.ATest": {"ok": True}})
    monkeypatch.setenv("HARNESS_RESUMED", "true")
    assert BL.ensure(new_run(), repo, repo / ".harness", log=quiet) is True
    assert builds(repo) == 1 and not (repo / "metrics" / "baseline").exists()


def test_saved_result_is_read_from_the_metrics_branch(repo, tmp_path):
    """The result another run published to harness-metrics is found by a fresh
    checkout that has no local copy."""
    plan(repo, tests={"p.ATest": {"ok": True, "bad": False}})
    assert BL.ensure(new_run(), repo, repo / ".harness", log=quiet) is True
    rec_file = next((repo / "metrics" / "baseline").glob("*.json"))
    # Publish it the way the workflow does: a commit on harness-metrics.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    pub = tmp_path / "pub"
    subprocess.run(["git", "clone", "-q", str(origin), str(pub)], check=True,
                   capture_output=True)
    _git(pub, "checkout", "-q", "--orphan", BL.METRICS_BRANCH)
    (pub / "metrics" / "baseline").mkdir(parents=True)
    (pub / "metrics" / "baseline" / rec_file.name).write_text(rec_file.read_text())
    _git(pub, "config", "user.email", "t@example.test")
    _git(pub, "config", "user.name", "t")
    _git(pub, "add", "-A")
    _git(pub, "commit", "-q", "-m", "metrics")
    _git(pub, "push", "-q", "origin", BL.METRICS_BRANCH)
    # A later run: local copy gone, same base commit.
    import shutil
    shutil.rmtree(repo / "metrics")
    run = new_run()
    assert BL.ensure(run, repo, repo / ".harness", log=quiet) is True
    assert run.baseline["reused"] is True and builds(repo) == 1
    assert BL.known_failing(run) == ["p.ATest#bad"]


# ------------------------------------------------- what later phases are told
def test_prompts_and_pr_body_name_the_already_failing_tests(repo):
    run = new_run()
    assert BL.prompt_block(run, "coding") == ""
    run.baseline = {"status": "tolerated", "failing_tests": ["p.ATest#bad", "p.CTest#x"],
                    "base_commit": "abc123def4567890", "still_failing": ["p.ATest#bad"]}
    for phase in ("coding", "unit_testing", "code_review"):
        b = BL.prompt_block(run, phase)
        assert "p.ATest#bad" in b and "Do NOT edit or delete these tests" in b
    assert "out of scope" in BL.prompt_block(run, "code_review")
    assert "already failing" in BL.prompt_block(run, "raise_pr")
    assert BL.prompt_block(run, "context") == ""

    body = repo / ".harness" / "pr-body.md"
    body.write_text("# Change\n")
    assert BL.add_to_pr_body(repo, run) is True
    text = body.read_text()
    assert "## Tests already failing before this change" in text
    assert "- `p.ATest#bad`\n" in text
    assert "- `p.CTest#x` — passes after this change" in text
    assert BL.add_to_pr_body(repo, run) is False          # added once


def test_metrics_record_carries_the_baseline_result():
    import metrics
    run = new_run()
    run.baseline = {"status": "tolerated", "failing_tests": ["a#b", "c#d"], "reused": True}
    rec = metrics.build_record(run, Path("."), None, log=quiet)
    assert rec["schema_version"] == 9
    assert rec["baseline_status"] == "tolerated"
    assert rec["baseline_failing_tests"] == 2 and rec["baseline_reused"] is True
    assert metrics.build_record(new_run(), Path("."), None, log=quiet)["baseline_status"] is None


# ---------------------------------------------- through the state machine
class _OkExecutor:
    """A phase that does its work without error (no SDK)."""
    def __init__(self, repo):
        self.repo_root = repo

    def run_phase(self, phase, run):
        from contracts import ExitCode
        return ExitCode.OK


def _at_unit_testing(repo, known):
    from state_machine import StateMachine
    run = RunState(feature_id="OP-1", current_phase="unit_testing",
                   story="s\n**Requires a new or changed contract from:** None")
    run.completed_phases = ["context", "prompt_steps", "coding", "code_review"]
    run.baseline = {"status": "tolerated", "failing_tests": list(known)}
    sm = StateMachine(_OkExecutor(repo), repo / ".harness", repo_root=repo, log=quiet)
    return sm, run


def test_state_machine_advances_past_already_failing_tests(repo):
    plan(repo, tests={"p.ATest": {"bad": False}, "p.BTest": {"ok": True}})
    sm, run = _at_unit_testing(repo, ["p.ATest#bad"])
    run = sm.step(run)
    assert run.status != "halted", run.last_feedback
    assert run.current_phase != "unit_testing" and run.validation_attempts == 0
    assert run.baseline["still_failing"] == ["p.ATest#bad"]


def test_state_machine_sends_back_only_the_new_failure(repo):
    plan(repo, tests={"p.ATest": {"bad": False}, "p.BTest": {"broke": False}})
    sm, run = _at_unit_testing(repo, ["p.ATest#bad"])
    run = sm.step(run)
    assert run.validation_attempts == 1 and run.status == "running"
    assert run.current_phase in ("coding", "unit_testing")
    assert "BTest.broke" in run.last_feedback
    assert "already fail on the base branch" in run.last_feedback
    assert "boom in p.ATest.bad" not in run.last_feedback
