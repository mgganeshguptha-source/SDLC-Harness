"""Guards added after petclinic-rest-api PC-101 (2026-11-01): broken tests the
harness wrote were routed to 'coding', which then bent the API contract to fit
them. Covers test-side classification, failing-test parsing, root-cause
extraction, the resume run-cap reset and the contract hash used by the
fix-loop freeze."""
from pathlib import Path

import state_machine as SM
import validation as V
from resume import rewind
from state import RunState


def _repo(tmp_path: Path) -> Path:
    t = tmp_path / "src/test/java/org/x"
    m = tmp_path / "src/main/java/org/x"
    t.mkdir(parents=True)
    m.mkdir(parents=True)
    (t / "VisitStatusBehaviorTest.java").write_text("class VisitStatusBehaviorTest {}")
    (m / "VisitController.java").write_text("class VisitController {}")
    return tmp_path


# Surefire-style report text: exception thrown from a TEST helper.
HELPER_THROWS = """\
Tests run: 5, Failures: 0, Errors: 1, Skipped: 0 <<< FAILURE! -- in org.x.VisitStatusBehaviorTest
[ERROR] org.x.VisitStatusBehaviorTest.shouldRejectInvalid -- Time elapsed: 0.002 s <<< ERROR!
java.lang.IllegalArgumentException: No enum constant org.x.dto.VisitStatusDto.DONE
\tat java.base/java.lang.Enum.valueOf(Enum.java:273)
\tat org.x.VisitStatusBehaviorTest.createDto(VisitStatusBehaviorTest.java:138)
\tat org.x.VisitStatusBehaviorTest.shouldRejectInvalid(VisitStatusBehaviorTest.java:114)
"""

# Same exception thrown by PRODUCTION code.
MAIN_THROWS = """\
[ERROR] org.x.VisitStatusBehaviorTest.shouldRejectInvalid -- Time elapsed: 0.002 s <<< ERROR!
java.lang.IllegalArgumentException: bad
\tat java.base/java.lang.Enum.valueOf(Enum.java:273)
\tat org.x.VisitController.parse(VisitController.java:40)
\tat org.x.VisitStatusBehaviorTest.shouldRejectInvalid(VisitStatusBehaviorTest.java:114)
"""

ASSERTION = """\
[ERROR] org.x.VisitStatusBehaviorTest.shouldDefineEnum -- Time elapsed: 0.02 s <<< FAILURE!
org.opentest4j.AssertionFailedError: Expecting value to be true but was false
\tat org.x.VisitStatusBehaviorTest.shouldDefineEnum(VisitStatusBehaviorTest.java:28)
"""


def test_mockito_short_name_is_test_side():
    out = ("[ERROR]   VisitStatusUnitTest.shouldThrow:93 » WrongTypeOfReturnValue\n"
           "[ERROR] Tests run: 4, Failures: 0, Errors: 2")
    assert SM._failure_is_in_tests(out) is True


def test_exception_thrown_by_test_helper_is_test_side(tmp_path):
    assert SM._failure_is_in_tests(HELPER_THROWS, _repo(tmp_path)) is True


def test_exception_thrown_by_production_stays_main_side(tmp_path):
    assert SM._failure_is_in_tests(MAIN_THROWS, _repo(tmp_path)) is False


def test_assertion_failure_stays_main_side(tmp_path):
    assert SM._failure_is_in_tests(ASSERTION, _repo(tmp_path)) is False


def test_frame_rule_needs_repo_root():
    # Without the repo the harness cannot tell test frames from production ones.
    assert SM._failure_is_in_tests(HELPER_THROWS) is False


def test_failing_test_classes_and_methods():
    out = HELPER_THROWS + "\n[ERROR]   OpenApiContractTest.shouldDefine:28 \n"
    assert SM._failing_test_classes(out) == ["OpenApiContractTest", "VisitStatusBehaviorTest"]
    assert SM._failing_test_methods(out) == ["VisitStatusBehaviorTest.shouldRejectInvalid"]


def test_root_cause_kept_and_frames_condensed():
    frames = "".join(f"\tat org.springframework.Foo.m{i}(Foo.java:{i})\n" for i in range(60))
    report = (
        "org.x.SpringConfigTests.contextLoads -- Time elapsed: 0.004 s <<< ERROR!\n"
        "java.lang.IllegalStateException: Failed to load ApplicationContext\n" + frames +
        "Caused by: org.springframework.jdbc.ScriptStatementFailedException: statement #44\n"
        + frames +
        "Caused by: java.sql.SQLSyntaxErrorException: row column count mismatch\n" + frames
    )
    assert V._root_causes(report) == [
        "Caused by: java.sql.SQLSyntaxErrorException: row column count mismatch"]
    short = V._condense_report(report)
    assert "row column count mismatch" in short
    assert "more frame(s) omitted by harness" in short
    assert len(short) < len(report) / 3


def test_resume_resets_run_cap_for_earlier_phases():
    run = RunState(feature_id="PC-101", story="s", current_phase="unit_testing")
    run.completed_phases = ["repo_instructions", "context", "design", "prompt_steps",
                            "coding", "code_review", "unit_testing"]
    run.iterations = {"__runs__:coding": 3, "coding": 5, "__runs__:unit_testing": 2}
    run.contract_snapshot = {"src/main/resources/openapi.yml": "abc"}
    rewind(run, "unit_testing", log=lambda *_: None)
    assert run.iterations["__runs__:coding"] == 0       # earlier phase: cap reset
    assert run.iterations["coding"] == 5                # its iteration history kept
    assert run.iterations["__runs__:unit_testing"] == 0
    assert run.contract_snapshot == {}


def test_contract_hashes_detect_a_change(tmp_path):
    spec = tmp_path / "src/main/resources/openapi.yml"
    spec.parent.mkdir(parents=True)
    spec.write_text("openapi: 3.0.1\n")

    class _Cfg:
        api_spec_files = ["src/main/resources/openapi.yml"]

        def _api_specs(self):
            return list(self.api_spec_files)

    sm = SM.StateMachine.__new__(SM.StateMachine)
    sm.repo_root = tmp_path
    sm.harness_dir = tmp_path / ".harness"
    before = sm._contract_hashes(_Cfg())
    spec.write_text("openapi: 3.0.1\n# loosened\n")
    after = sm._contract_hashes(_Cfg())
    assert before.keys() == after.keys()
    assert before != after


def test_state_loads_old_run_state_without_snapshot(tmp_path):
    # run-state.json written before this field existed must still load.
    import json
    hd = tmp_path / ".harness"
    hd.mkdir()
    old = RunState(feature_id="PC-1", story="s", current_phase="context")
    data = old.__dict__.copy()
    data.pop("contract_snapshot")
    (hd / "run-state.json").write_text(json.dumps(data))
    assert RunState.load(hd).contract_snapshot == {}
