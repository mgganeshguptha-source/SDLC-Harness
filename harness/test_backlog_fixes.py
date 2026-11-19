"""Backlog fixes, 2026-11-16:
  - a failed build's summary names its cause (not just "BUILD FAILURE");
  - "AI usage: none" when no AI phase ran;
  - the unit_testing prompt carries the existing test class of the changed
    code, wherever it lives in the test tree;
  - the AC check is given the files the run changed, tests included;
  - PR wording for one already-failing test;
  - contract-only mode is gone.
"""
import subprocess
import sys
from pathlib import Path

import pytest

import baseline as BL
import run as R
import sdk_runner as SR
from state import RunState
from validation import first_cause, with_cause

COMPILE = """[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile (default-compile) on project app: Compilation failure
[ERROR] /home/runner/work/app/app/service/src/main/java/a/model/PetType.java:[45,16] cannot find symbol
[ERROR]   symbol:   class Missing
[ERROR] -> [Help 1]
"""
MISSING_LIB = ("[ERROR] Failed to execute goal on project app: Could not resolve dependencies "
               "for project a:app:jar:1.0: Could not find artifact a:client:jar:1.0-SNAPSHOT "
               "-> [Help 1]\n[INFO] BUILD FAILURE\n")


# ------------------------------------------------------------- build cause
def test_compile_error_is_named_with_file_and_line():
    assert with_cause("BUILD FAILURE", COMPILE) == \
        "BUILD FAILURE — PetType.java:[45,16] cannot find symbol"


def test_missing_library_is_named():
    cause = first_cause(MISSING_LIB)
    assert "Could not find artifact a:client:jar:1.0-SNAPSHOT" in cause
    assert "Help" not in cause


def test_no_cause_found_leaves_the_summary_alone_and_long_ones_are_cut():
    assert with_cause("BUILD FAILURE", "[INFO] BUILD FAILURE") == "BUILD FAILURE"
    long = "[ERROR] " + "x" * 400
    assert len(first_cause(long)) <= 180 and first_cause(long).endswith("...")


def test_saved_result_without_a_cause_gets_one_at_the_halt(tmp_path):
    run = RunState(feature_id="F-1", story="s", current_phase="context")
    seen = []
    rec = {"status": "failed", "summary": "BUILD FAILURE", "detail": COMPILE,
           "failing_tests": []}
    BL._halt(run, rec, tmp_path, seen.append, reused=True)
    assert "Detail : BUILD FAILURE — PetType.java:[45,16] cannot find symbol" in seen[0]
    assert "PetType.java" in run.halt_detail


# ---------------------------------------------------------------- AI usage
def test_ai_usage_says_none_when_no_ai_phase_ran(capsys):
    R._report_sdk_cost(RunState(feature_id="F-1", story="s", current_phase="context"))
    out = capsys.readouterr().out
    assert "AI usage: none — no AI phase ran in this run." in out
    assert "not reported" not in out


# ------------------------------------------- existing test class is inlined
def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


@pytest.fixture
def java_repo(tmp_path):
    repo = tmp_path / "svc"
    main = repo / "src/main/java/a/rest/controller/v1"
    main.mkdir(parents=True)
    (main / "PetTypeController.java").write_text("class PetTypeController {}\n")
    # The test lives one folder UP from the class it tests.
    test = repo / "src/test/java/a/rest/controller"
    test.mkdir(parents=True)
    (test / "PetTypeControllerTests.java").write_text(
        "class PetTypeControllerTests { void existingCase() {} }\n")
    (test / "OwnerControllerTests.java").write_text("class OwnerControllerTests {}\n")
    (repo / ".harness").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (main / "PetTypeController.java").write_text("class PetTypeController { void trim() {} }\n")
    return repo


def test_test_class_in_another_folder_is_found_by_name(java_repo):
    found = SR._existing_tests_for(java_repo, java_repo / "src/test",
                                   ["src/main/java/a/rest/controller/v1/PetTypeController.java"])
    assert [f.name for f in found] == ["PetTypeControllerTests.java"]


def test_test_file_named_in_the_plan_is_found(java_repo):
    (java_repo / ".harness/prompt-steps.md").write_text(
        "Add cases to `src/test/java/a/rest/controller/OwnerControllerTests.java`.\n"
        "Create src/test/java/a/rest/controller/NewTests.java\n")
    found = SR._existing_tests_for(java_repo, java_repo / "src/test",
                                   ["src/main/java/a/rest/controller/v1/PetTypeController.java"])
    assert [f.name for f in found] == ["PetTypeControllerTests.java",
                                       "OwnerControllerTests.java"]


def test_dossier_carries_the_existing_test_class(java_repo):
    d = SR._build_unittest_dossier(java_repo, log=lambda *a: None)
    assert "EXISTING TEST CLASS FOR THE CODE UNDER TEST" in d
    assert "void existingCase()" in d
    # Not repeated as a sibling test.
    assert d.count("class PetTypeControllerTests") == 1


# --------------------------------------------- AC check sees the changed files
def test_ac_check_is_given_new_test_classes(java_repo, monkeypatch):
    monkeypatch.delenv("HARNESS_BASE_BRANCH", raising=False)
    new = java_repo / "src/test/java/a/rest/controller/PetTypeControllerTrimTests.java"
    new.write_text("class PetTypeControllerTrimTests {}\n")
    (java_repo / ".harness/validation.md").write_text("x")
    block = SR._run_changes_block(java_repo, None)
    assert "  test: src/test/java/a/rest/controller/PetTypeControllerTrimTests.java" in block
    assert ("  production / other: src/main/java/a/rest/controller/v1/PetTypeController.java"
            in block)
    assert ".harness/" not in block and "ANY test file on this list" in block


def test_no_changes_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_BASE_BRANCH", raising=False)
    _git(tmp_path, "init", "-q")
    assert SR._run_changes_block(tmp_path, None) == ""


# ------------------------------------------------------------ PR wording
@pytest.mark.parametrize("tests,expected,absent", [
    (["a.T#one"], "This test failed on the base branch", "test(s)"),
    (["a.T#one", "a.T#two"], "These 2 tests failed on the base branch", "test(s)"),
])
def test_pr_wording_for_already_failing_tests(tmp_path, tests, expected, absent):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/pr-body.md").write_text("# PR\n")
    run = RunState(feature_id="F-1", story="s", current_phase="raise_pr")
    run.baseline = {"status": "tolerated", "failing_tests": tests, "base_commit": "abc"}
    assert BL.add_to_pr_body(tmp_path, run) is True
    body = (tmp_path / ".harness/pr-body.md").read_text()
    assert expected in body and absent not in body


# ------------------------------------------------------- contract-only gone
def test_contract_command_no_longer_exists(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "contract", "--repo", ".", "--feature", "F",
                                      "--base", "b"])
    with pytest.raises(SystemExit):
        R.main()
    assert not hasattr(R, "cmd_contract")


def test_workflow_no_longer_passes_runs_build_or_runs_contract_only():
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/harness.yml").read_text()
    assert "HARNESS_RUNS_BUILD" not in wf
    assert "run.py contract" not in wf
