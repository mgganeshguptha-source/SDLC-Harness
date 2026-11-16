"""
test_fix_loop_controls.py - the compile check after coding (compile_check.py)
and the file limit in a build-fix loop (fix_scope.py).

Both come from OP018-6434 (2026-11-15): code that did not compile was reviewed
and had tests written for it, and coding, sent back with failing tests, edited
five production files the plan never listed.

Run:  python -m pytest harness/test_fix_loop_controls.py -v
"""
import subprocess
import sys
from pathlib import Path

import pytest

import compile_check as CC
import fix_scope as FS
import halt_gates as HG
from contracts import ExitCode
from executor import AgentResult, PhaseExecutor
from phases import PHASES
from state import RunState
from state_machine import StateMachine

quiet = lambda *a, **k: None
STORY = "s\n**Requires a new or changed contract from:** None"
MAIN = "src/main/java/p"
PLANNED = f"{MAIN}/Planned.java"
OTHER = f"{MAIN}/Other.java"          # exists, not in the plan
CALLER = f"{MAIN}/Caller.java"        # exists, not in the plan


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for v in ("HARNESS_RESUMED", "HARNESS_RUNS_BUILD", "MAVEN_OPTS", "MAVEN_ARGS"):
        monkeypatch.delenv(v, raising=False)
    r = tmp_path / "service"
    (r / MAIN).mkdir(parents=True)
    (r / ".harness").mkdir()
    for f in (PLANNED, OTHER, CALLER):
        (r / f).write_text(f"class {Path(f).stem} {{}}\n")
    (r / ".harness" / "prompt-steps.md").write_text(
        "# Plan\n\n## Impacted Files\n| ID | Path | Role |\n|---|---|---|\n"
        f"| F1 | {PLANNED} | change |\n\n## Steps\n1. do it\n")
    (r / ".gitignore").write_text(".harness/\nbuild-result.txt\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.test")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


# =================================================================== item 2
def test_compile_command_is_derived_from_a_plain_maven_test_command():
    assert CC.derive("mvn -U -T 1C -s $MAVEN_SETTINGS -e test") == \
        "mvn -U -T 1C -s $MAVEN_SETTINGS -e compile"
    assert CC.derive("./mvnw clean verify -Dtest=FooTest") == "./mvnw clean compile"
    assert CC.derive("mvnw.cmd test") == "mvnw.cmd compile"
    # Not a single Maven call: cannot be derived.
    assert CC.derive("npm install && npx ng build") is None
    assert CC.derive("mvn test | tee out.txt") is None
    assert CC.derive("mvn -v") is None
    assert CC.derive("") is None


def _compile_setup(repo, tmp_path, outcome, extra=""):
    script = tmp_path / "fakecompile.py"
    script.write_text(
        "import sys\n"
        f"o = open(r'{repo}/build-result.txt').read().strip()\n"
        "if o == 'ok': print('[INFO] BUILD SUCCESS'); sys.exit(0)\n"
        "if o == 'missing':\n"
        "    print('[ERROR] Failed to execute goal on project app: Could not resolve "
        "dependencies: Could not find artifact p:lib:jar:2.0 in store'); sys.exit(1)\n"
        "print('[ERROR] COMPILATION ERROR :')\n"
        f"print('[ERROR] /w/service/{PLANNED}:[3,9] error: cannot find symbol')\n"
        "print('[INFO] BUILD FAILURE'); sys.exit(1)\n")
    (repo / "build-result.txt").write_text(outcome)
    (repo / ".harness" / "config.yaml").write_text(
        f'test_command: "mvn test"\ncompile_command: "{sys.executable} {script}"\n'
        f'min_coverage: 0\npre_validation_command: ""\n{extra}')


class _Ok:
    def __init__(self, repo):
        self.repo_root = repo

    def run_phase(self, phase, run):
        return ExitCode.OK


def _after_coding(repo):
    run = RunState(feature_id="OP-1", story=STORY, current_phase="coding")
    run.completed_phases = ["context", "prompt_steps"]
    run.changed_main_files = [PLANNED]
    return StateMachine(_Ok(repo), repo / ".harness", repo_root=repo, log=quiet), run


def test_code_that_compiles_goes_on_to_review(repo, tmp_path):
    _compile_setup(repo, tmp_path, "ok")
    sm, run = _after_coding(repo)
    run = sm.step(run)
    assert run.status != "halted" and run.compile_attempts == 0
    assert run.approvals.get("coding") != "rejected"


def test_compile_error_goes_back_to_coding_before_review(repo, tmp_path):
    _compile_setup(repo, tmp_path, "error")
    sm, run = _after_coding(repo)
    run = sm.step(run)
    assert run.current_phase == "coding" and run.status == "running"
    assert run.approvals["coding"] == "rejected" and run.compile_attempts == 1
    assert run.fix_loop == "build"
    assert "does NOT COMPILE" in run.last_feedback
    assert "cannot find symbol" in run.last_feedback
    assert "code_review" not in run.completed_phases


def test_compile_error_halts_when_the_budget_is_used_up(repo, tmp_path):
    _compile_setup(repo, tmp_path, "error", "max_validation_retries: 1\n")
    sm, run = _after_coding(repo)
    run = sm.step(run)                       # attempt 1: sent back
    assert run.status == "running"
    run.approvals.pop("coding", None)
    run = sm.step(run)                       # attempt 2: budget used up
    assert run.status == "halted" and run.halt_gate == HG.TEST_BUILD
    assert "COMPILE CHECK: HALTED" in run.last_feedback


def test_missing_library_at_the_compile_check_is_an_environment_halt(repo, tmp_path):
    _compile_setup(repo, tmp_path, "missing")
    sm, run = _after_coding(repo)
    run = sm.step(run)
    assert run.status == "halted" and run.halt_gate == HG.CONFIG_ERROR
    assert run.compile_attempts == 0


def test_compile_check_is_skipped_when_it_does_not_apply(repo, tmp_path, monkeypatch):
    _compile_setup(repo, tmp_path, "error")
    sm, run = _after_coding(repo)
    monkeypatch.setenv("HARNESS_RUNS_BUILD", "false")
    assert sm.step(run).compile_attempts == 0
    monkeypatch.delenv("HARNESS_RUNS_BUILD")
    # "off" disables it; a non-Maven test_command with no compile_command skips it.
    (repo / ".harness" / "config.yaml").write_text(
        'test_command: "mvn test"\ncompile_command: "off"\nmin_coverage: 0\n')
    sm, run = _after_coding(repo)
    assert sm.step(run).compile_attempts == 0
    (repo / ".harness" / "config.yaml").write_text(
        'test_command: "npm install && npx ng build"\nmin_coverage: 0\n')
    sm, run = _after_coding(repo)
    assert sm.step(run).compile_attempts == 0


# =================================================================== item 3
MAVEN_COMPILE_ERROR = (
    "--- MAVEN ERRORS ---\n"
    f"[ERROR] /home/runner/work/x/x/service/{CALLER}:[122,16] error: cannot find symbol\n"
    "[ERROR] Failed to execute goal maven-compiler-plugin:3.11.0:compile\n")
TEST_FAILURE = (
    "[ERROR] p.OtherTest.checks -- Time elapsed: 0.01 s <<< ERROR!\n"
    "java.lang.NullPointerException: activeUserService is null\n"
    "\tat p.Other.run(Other.java:68)\n"
    "\tat p.OtherTest.checks(OtherTest.java:40)\n")


def test_only_compiler_locations_name_a_file():
    assert FS.compile_error_files(MAVEN_COMPILE_ERROR) == {
        f"/home/runner/work/x/x/service/{CALLER}"}
    assert FS.compile_error_files(
        "Error: src/app/a.component.ts:12:5 - error TS2322: bad type") == {
        "src/app/a.component.ts"}
    assert FS.compile_error_files("Foo.java:12: error: ';' expected") == {"Foo.java"}
    # A stack trace passes through files; it does not name them.
    assert FS.compile_error_files(TEST_FAILURE) == set()
    # A warning is not an error.
    assert FS.compile_error_files(f"[WARNING] /w/{OTHER}:[3,1] deprecated") == set()


def test_blocked_edits_rule():
    # Already changed by the run: allowed. Named by the compiler: allowed.
    assert FS.blocked_edits([OTHER], {OTHER}, TEST_FAILURE) == []
    assert FS.blocked_edits([CALLER], set(), MAVEN_COMPILE_ERROR) == []
    # Reached only through a failing test's stack trace: blocked.
    assert FS.blocked_edits([OTHER, CALLER], set(), TEST_FAILURE) == [CALLER, OTHER]
    assert FS.blocked_edits([OTHER, CALLER], set(), MAVEN_COMPILE_ERROR) == [OTHER]


class _Writes:
    """A coding agent that edits the given files."""
    def __init__(self, files):
        self.files = files

    def run(self, phase, prompt, run, repo_root):
        for f in self.files:
            p = Path(repo_root) / f
            p.write_text(p.read_text() + "// edited by the agent\n")
        return AgentResult(attempted_writes=[str(Path(repo_root) / f) for f in self.files])


def _coding_pass(repo, files, fix_loop, feedback, changed_before=(PLANNED,)):
    ex = PhaseExecutor(runner=_Writes(files), repo_root=repo,
                       harness_dir=repo / ".harness", log=quiet)
    run = RunState(feature_id="OP-1", story=STORY, current_phase="coding")
    run.changed_main_files = list(changed_before)
    run.fix_loop = fix_loop
    if feedback:
        run.last_feedback = feedback
        run.approvals["coding"] = "rejected"
    coding = next(p for p in PHASES if p.id == "coding")
    return ex.run_phase(coding, run), run


def _edited(repo, f) -> bool:
    return "edited by the agent" in (repo / f).read_text()


def test_first_pass_may_still_edit_an_unplanned_existing_file(repo):
    code, run = _coding_pass(repo, [PLANNED, OTHER], "", None, changed_before=())
    assert code == ExitCode.OK and _edited(repo, OTHER)


def test_build_fix_edit_outside_the_set_is_reverted_and_halts(repo):
    code, run = _coding_pass(repo, [PLANNED, OTHER], "build", TEST_FAILURE)
    assert code == ExitCode.FIX_SCOPE_VIOLATION
    assert not _edited(repo, OTHER)             # reverted
    assert _edited(repo, PLANNED)               # the planned file keeps its fix
    assert OTHER not in run.changed_main_files
    assert "UNPLANNED EDIT" in run.last_feedback and OTHER in run.last_feedback
    patch = (repo / ".harness" / FS.PATCH_FILE).read_text()
    assert "edited by the agent" in patch and "Other.java" in patch
    # The state machine halts on it — no redo.
    class _Ex:
        repo_root = repo
        def run_phase(self, phase, r):
            return ExitCode.FIX_SCOPE_VIOLATION
    sm = StateMachine(_Ex(), repo / ".harness", repo_root=repo, log=quiet)
    run.completed_phases = ["context", "prompt_steps"]
    run = sm.step(run)
    assert run.status == "halted" and run.halt_gate == HG.SCOPE


def test_build_fix_may_edit_a_file_the_compiler_names(repo):
    code, run = _coding_pass(repo, [PLANNED, CALLER], "build", MAVEN_COMPILE_ERROR)
    assert code == ExitCode.OK and _edited(repo, CALLER)
    assert CALLER in run.changed_main_files


def test_build_fix_may_edit_a_file_the_run_already_changed(repo):
    code, run = _coding_pass(repo, [OTHER], "build", TEST_FAILURE,
                             changed_before=(PLANNED, OTHER))
    assert code == ExitCode.OK and _edited(repo, OTHER)


def test_a_reviewer_ordered_fix_is_not_limited(repo):
    """PC-101: the reviewer asked for a fix in a class the plan had not listed."""
    code, run = _coding_pass(repo, [OTHER], "review",
                             "Reviewer issues:\n- (CORRECTNESS) Other.run ignores null")
    assert code == ExitCode.OK and _edited(repo, OTHER)


def test_coding_is_told_the_rule_only_in_a_build_fix_loop():
    run = RunState(feature_id="OP-1", story=STORY, current_phase="coding")
    run.changed_main_files = [PLANNED]
    assert FS.prompt_block(run, "coding") == ""
    run.fix_loop, run.last_feedback = "build", TEST_FAILURE
    run.approvals["coding"] = "rejected"
    b = FS.prompt_block(run, "coding")
    assert PLANNED in b and "Do NOT edit any other existing production file" in b
    run.fix_loop = "review"
    assert FS.prompt_block(run, "coding") == ""


def test_resume_clears_the_fix_loop_state(tmp_path):
    from resume import rewind
    run = RunState(feature_id="OP-1", story=STORY, current_phase="coding")
    run.status, run.fix_loop, run.compile_attempts = "halted", "build", 2
    run = rewind(run, "coding", log=quiet)
    assert run.fix_loop == "" and run.compile_attempts == 0
