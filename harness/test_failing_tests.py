"""
test_failing_tests.py — tests that already fail on the base branch
(failing_tests.py): Java method spans, scope mapping, triage parsing, the
guard (put back / send back / record), the opt-in repair, the build gate list,
prompts, PR lines, and the guard through the state machine.

No SDK, no Maven, no network.

Run:  python -m pytest test_failing_tests.py -v
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import baseline as BL
import failing_tests as FT
import unit_test_mode as UT
from contracts import ExitCode
from state import RunState
from state_machine import StateMachine

quiet = lambda *a, **k: None

TEST_SRC = '''package com.x.claims;

import org.junit.jupiter.api.Test;

class ClaimServiceTest {

    private final ClaimService svc = new ClaimService();

    @Test
    void mapsName() {
        // a brace in a comment: {
        String s = "a } brace in a string";
        assertEquals("Alex", svc.name());
    }

    @Test
    @DisplayName("formats the key")
    void formatsKey() throws Exception {
        assertEquals("P-1", svc.key('}'));
        helper();
    }

    private void helper() {
        mapsName();
    }
}
'''

PROD = "package com.x.claims;\nclass ClaimService {}\n"
OTHER = "package com.x.claims;\nclass ClaimMapper {}\n"

ID_MAP = "com.x.claims.ClaimServiceTest#mapsName"
ID_KEY = "com.x.claims.ClaimServiceTest#formatsKey"


def _repo(extra_cfg: str = "") -> Path:
    root = Path(tempfile.mkdtemp())
    files = {
        "src/main/java/com/x/claims/ClaimService.java": PROD,
        "src/main/java/com/x/claims/ClaimMapper.java": OTHER,
        "src/test/java/com/x/claims/ClaimServiceTest.java": TEST_SRC,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / ".harness").mkdir()
    (root / ".harness" / "config.yaml").write_text("attachment_pii_scan: false\n" + extra_cfg,
                                                   encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _test_file(root):
    return root / "src/test/java/com/x/claims/ClaimServiceTest.java"


def _ut_run(known, repair=False, targets=None) -> RunState:
    r = RunState(feature_id="UT-1", story="add tests", current_phase="unit_testing",
                 mode="unit-test")
    r.baseline = {"status": "tolerated", "failing_tests": list(known)}
    r.unit_test = {"targets": targets or ["src/main/java/com/x/claims/ClaimService.java"],
                   "min_coverage": 90.0, "metric": "LINE"}
    return r


def _prepare(root, run, repair):
    s = UT.Settings(repair_failing=repair)
    return FT.prepare(run, root, ["src/main/java/com/x/claims/ClaimService.java",
                                  "src/main/java/com/x/claims/ClaimMapper.java"],
                      BL.known_failing(run), s, quiet)


# ------------------------------------------------------------ java methods
def test_method_span_skips_comments_strings_calls_and_keeps_annotations():
    t = FT.method_text(TEST_SRC, "mapsName")
    assert t.lstrip().startswith("@Test") and t.rstrip().endswith("}")
    assert '"a } brace in a string"' in t and "assertEquals(\"Alex\"" in t
    assert "formatsKey" not in t
    k = FT.method_text(TEST_SRC, "formatsKey")
    assert "@DisplayName" in k and "helper();" in k and "throws Exception" in k
    # helper() calls mapsName(); the call is not taken for the declaration
    assert FT.method_text(TEST_SRC, "helper").count("mapsName();") == 1
    assert FT.method_text(TEST_SRC, "missing") is None


def test_ids_and_production_mapping():
    assert FT.split_id("com.x.ATest#shouldMap(String)[1]") == ("com.x.ATest", "shouldMap")
    assert FT.production_key("com.x.ClaimServiceTest") == ("com.x", "ClaimService")
    assert FT.production_key("com.x.ClaimServiceIT$Nested") == ("com.x", "ClaimService")
    assert FT.production_key("com.x.Helper") == ("", "")
    scope = FT.in_scope([ID_MAP, "com.y.OtherTest#a"],
                        ["src/main/java/com/x/claims/ClaimService.java"])
    assert scope == {ID_MAP: "src/main/java/com/x/claims/ClaimService.java"}


def test_test_file_found_in_a_module():
    root = Path(tempfile.mkdtemp())
    p = root / "svc-application/src/test/java/com/x/ATest.java"
    p.parent.mkdir(parents=True)
    p.write_text("class ATest {}")
    assert FT.test_file_for(root, "com.x.ATest") == "svc-application/src/test/java/com/x/ATest.java"
    assert FT.test_file_for(root, "com.x.ATest$Inner") == "svc-application/src/test/java/com/x/ATest.java"


def test_read_triage():
    root = _repo()
    (root / FT.TRIAGE_FILE).write_text(
        f"# triage\n- {ID_MAP}: STALE — field renamed to displayName\n"
        f"- `{ID_KEY}`: defect - key drops the DOB\n- com.x.A#b: MAYBE — ?\n")
    t = FT.read_triage(root)
    assert t[ID_MAP] == ("STALE", "field renamed to displayName")
    assert t[ID_KEY][0] == "DEFECT"
    assert "com.x.A#b" not in t


# --------------------------------------------------------------- the guard
def _edit(root, name, new_assert):
    f = _test_file(root)
    src = f.read_text()
    old = FT.method_text(src, name)
    f.write_text(src.replace(old, old.replace("assertEquals", new_assert, 1)))


def test_unit_test_mode_puts_back_an_edit_when_repair_is_off():
    root = _repo()
    run = _ut_run([ID_MAP, ID_KEY])
    _prepare(root, run, repair=False)
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    original = _test_file(root).read_text()
    _edit(root, "mapsName", "assertNotNull")
    # a new test added in the same file stays
    f = _test_file(root)
    f.write_text(f.read_text().rstrip()[:-1] + "\n    @Test\n    void newOne() { }\n}\n")
    fb = FT.after_phase(run, root, "unit_testing", before, quiet)
    assert fb == ""
    now = f.read_text()
    assert FT.method_text(now, "mapsName") == FT.method_text(original, "mapsName")
    assert "void newOne()" in now
    assert run.unit_test["restored"] == [ID_MAP] and run.unit_test["repaired"] == []


def test_deleting_an_already_failing_test_sends_the_phase_back():
    root = _repo()
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=True)
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    f = _test_file(root)
    f.write_text(f.read_text().replace(FT.method_text(f.read_text(), "mapsName"), ""))
    fb = FT.after_phase(run, root, "unit_testing", before, quiet)
    assert "DELETED" in fb and ID_MAP in fb and "assertEquals(\"Alex\"" in fb


def test_stale_repair_is_kept_when_repair_is_on_and_must_then_pass():
    root = _repo()
    run = _ut_run([ID_MAP, ID_KEY])
    first = _prepare(root, run, repair=True)
    assert first == ["src/main/java/com/x/claims/ClaimService.java"]
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    _edit(root, "mapsName", "assertSame")          # marked STALE: allowed
    _edit(root, "formatsKey", "assertSame")        # marked DEFECT: put back
    (root / FT.TRIAGE_FILE).write_text(f"- {ID_MAP}: STALE — renamed field\n"
                                       f"- {ID_KEY}: DEFECT — key drops DOB\n")
    assert FT.after_phase(run, root, "unit_testing", before, quiet) == ""
    src = _test_file(root).read_text()
    assert "assertSame" in FT.method_text(src, "mapsName")
    assert "assertSame" not in FT.method_text(src, "formatsKey")
    assert run.unit_test["repaired"] == [ID_MAP]
    assert run.unit_test["restored"] == [ID_KEY]
    # the build gate no longer ignores the repaired test
    assert BL.gate_known_failing(run) == [ID_KEY]


def test_repair_without_a_stale_triage_line_is_put_back():
    root = _repo()
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=True)
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    _edit(root, "mapsName", "assertSame")
    FT.after_phase(run, root, "unit_testing", before, quiet)
    assert "assertSame" not in FT.method_text(_test_file(root).read_text(), "mapsName")
    assert run.unit_test["repaired"] == []


def test_repair_outside_the_batch_is_put_back():
    root = _repo()
    run = _ut_run([ID_MAP], targets=["src/main/java/com/x/claims/ClaimMapper.java"])
    _prepare(root, run, repair=True)
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    _edit(root, "mapsName", "assertSame")
    (root / FT.TRIAGE_FILE).write_text(f"- {ID_MAP}: STALE — x\n")
    FT.after_phase(run, root, "unit_testing", before, quiet)
    assert run.unit_test["restored"] == [ID_MAP] and run.unit_test["repaired"] == []


def test_build_mode_records_the_edit_and_reverts_nothing():
    root = _repo()
    run = RunState(feature_id="OP-1", story="s", current_phase="coding")
    run.baseline = {"status": "tolerated", "failing_tests": [ID_MAP]}
    assert FT.before_phase(run, root, "code_review", [ID_MAP]) is None
    before = FT.before_phase(run, root, "coding", [ID_MAP])
    _edit(root, "mapsName", "assertNotNull")
    assert FT.after_phase(run, root, "coding", before, quiet) == ""
    assert "assertNotNull" in FT.method_text(_test_file(root).read_text(), "mapsName")
    assert run.baseline["edited"] == [ID_MAP]
    body = root / ".harness" / "pr-body.md"
    body.write_text("# Change\n")
    BL.add_to_pr_body(root, run)
    assert "test edited by this change" in body.read_text()


def test_whitespace_only_edit_is_not_a_change():
    root = _repo()
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=False)
    before = FT.before_phase(run, root, "unit_testing", [ID_MAP])
    f = _test_file(root)
    f.write_text(f.read_text().replace("assertEquals(\"Alex\", svc.name());",
                                       "assertEquals(\"Alex\",  svc.name());"))
    assert FT.changes(root, before) == []


# --------------------------------------------------------- batch selection
def test_prepare_puts_repair_classes_first_in_the_batch(tmp_path):
    root = _repo("unit_test:\n  scope: repo\n  repair_failing: true\n"
                 "  max_classes_per_run: 1\n")
    run = _ut_run([ID_MAP])
    run.unit_test = {}
    # ClaimService has no coverage row; ClaimMapper is lower: without repair
    # the batch would be ClaimMapper.
    rep = root / "target/site/jacoco"
    rep.mkdir(parents=True)
    (rep / "jacoco.csv").write_text(
        "GROUP,PACKAGE,CLASS,LINE_MISSED,LINE_COVERED\n"
        "g,com.x.claims,ClaimService,0,10\n"
        "g,com.x.claims,ClaimMapper,10,0\n")
    assert UT.prepare(run, root, root / ".harness", log=quiet) is True
    assert run.unit_test["targets"] == ["src/main/java/com/x/claims/ClaimService.java"]
    assert run.unit_test["remaining"] == ["src/main/java/com/x/claims/ClaimMapper.java"]
    assert run.unit_test["repair_failing"] is True


def test_settings_reads_repair_failing():
    from config import HarnessConfig
    root = _repo("unit_test:\n  repair_failing: true\n")
    assert UT.settings(HarnessConfig.load(root / ".harness", repo_root=root)).repair_failing
    root = _repo()
    assert not UT.settings(HarnessConfig.load(root / ".harness", repo_root=root)).repair_failing


# ----------------------------------------------------------- prompts / PR
def test_unit_test_prompts():
    root = _repo()
    run = _ut_run([ID_MAP, "com.y.OtherTest#a"])
    _prepare(root, run, repair=False)
    p = BL.prompt_block(run, "unit_testing")
    assert "Do NOT edit or delete these tests" in p and "TRIAGE (required)" in p
    assert FT.TRIAGE_FILE in p and "Triage only" in p and "REPAIR (enabled" not in p
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=True)
    p = BL.prompt_block(run, "unit_testing")
    assert "REPAIR (enabled" in p and "never delete it" in p
    run.unit_test["repaired"] = [ID_MAP]
    r = BL.prompt_block(run, "code_review")
    assert "REPAIRED" in r and "(CONTRACT)" in r and "(CORRECTNESS)" in r
    # build mode keeps its own wording
    b = RunState(feature_id="OP-1", story="s", current_phase="coding")
    b.baseline = {"status": "tolerated", "failing_tests": [ID_MAP]}
    assert "unless the story itself requires" in BL.prompt_block(b, "unit_testing")


def test_review_material_and_pr_section():
    root = _repo()
    run = _ut_run([ID_MAP, ID_KEY])
    _prepare(root, run, repair=True)
    before = FT.before_phase(run, root, "unit_testing", BL.known_failing(run))
    _edit(root, "mapsName", "assertSame")
    (root / FT.TRIAGE_FILE).write_text(f"- {ID_MAP}: STALE — renamed field\n"
                                       f"- {ID_KEY}: DEFECT — key | drops DOB\n")
    FT.after_phase(run, root, "unit_testing", before, quiet)
    m = FT.review_material(root, run)
    assert "BEFORE (base branch, failing)" in m and "assertSame" in m and "assertEquals" in m
    run.baseline["still_failing"] = [ID_KEY]
    text = "\n".join(FT.pr_section(root, run))
    assert "### Already-failing tests in scope" in text
    assert f"| `{ID_MAP}` | STALE — renamed field | repaired — passes |" in text
    assert f"| `{ID_KEY}` | DEFECT — key / drops DOB | still failing — left as is |" in text
    full = UT.pr_section(root, run)
    assert "Already-failing tests in scope" in full


def test_metrics_fields():
    run = _ut_run([ID_MAP])
    run.unit_test.update({"failing_in_scope": {ID_MAP: "x"}, "repaired": [ID_MAP],
                          "repair_failing": True})
    f = FT.metrics_fields(run)
    assert f["unit_test_failing_in_scope"] == 1 and f["unit_test_failing_repaired"] == 1
    assert f["baseline_failing_edited"] == 0


# ------------------------------------------------- through the state machine
class _EditingExec:
    """unit_testing edits/deletes an already-failing test; other phases pass."""
    def __init__(self, root, action):
        self.repo_root = root
        self.action = action
        self.calls = []

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        if phase.id == "unit_testing" and self.calls.count("unit_testing") == 1:
            f = _test_file(self.repo_root)
            src = f.read_text()
            old = FT.method_text(src, "mapsName")
            if self.action == "delete":
                f.write_text(src.replace(old, ""))
            else:
                f.write_text(src.replace(old, old.replace("assertEquals", "assertNotNull", 1)))
        if phase.id == "code_review":
            (self.repo_root / ".harness" / "review.md").write_text("VERDICT: PASS\n")
        return ExitCode.OK


class _VR:
    passed, summary, exit_code, output_tail, failure_kind = True, "FAKE", 0, "", None
    coverage_pct = coverage_target = None
    coverage_classes = []


def _sm(root, action, monkeypatch):
    monkeypatch.setenv("HARNESS_CONFIRM_NO_PHI", "true")
    ex = _EditingExec(root, action)
    sm = StateMachine(ex, root / ".harness", repo_root=root, log=quiet,
                      validator=lambda *a: _VR())
    return sm, ex


def _drive(sm, run):
    for _ in range(30):
        run = sm.run_until_pause(run)
        if run.status == "awaiting_approval":
            run = sm.resolve_gate(run, approved=True)
        elif run.status in ("done", "halted", "needs_input"):
            break
    return run


def test_state_machine_puts_back_an_edit_and_carries_on(monkeypatch):
    root = _repo()
    sm, ex = _sm(root, "edit", monkeypatch)
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=False)
    run.save(root / ".harness")
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.last_feedback)
    assert ex.calls == ["unit_testing", "code_review", "raise_pr"]
    assert "assertNotNull" not in _test_file(root).read_text()
    assert run.unit_test["restored"] == [ID_MAP]


def test_state_machine_sends_back_a_deletion(monkeypatch):
    root = _repo()
    sm, ex = _sm(root, "delete", monkeypatch)
    run = _ut_run([ID_MAP])
    _prepare(root, run, repair=False)
    run.save(root / ".harness")
    run = sm.step(run)
    assert run.current_phase == "unit_testing" and run.status == "running"
    assert "DELETED" in run.last_feedback
    assert run.approvals["unit_testing"] == "rejected"
    # the next pass leaves it deleted: still caught, sent back again
    run = sm.step(run)
    assert run.current_phase == "unit_testing" and "DELETED" in run.last_feedback
    assert ex.calls == ["unit_testing", "unit_testing"]
    # restored by hand: the run carries on
    _test_file(root).write_text(TEST_SRC)
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.last_feedback)


# --------------------------------------- the gate with a repaired test (real)
FAKE_BUILD = r'''
import json, os, sys
from pathlib import Path
here = Path(sys.argv[1])
rep = here / "app" / "target" / "surefire-reports"
rep.mkdir(parents=True, exist_ok=True)
cls = "com.x.claims.ClaimServiceTest"
(rep / f"TEST-{cls}.xml").write_text(
    f'<testsuite name="{cls}" tests="2" failures="2">'
    f'<testcase name="mapsName" classname="{cls}"><failure>boom</failure></testcase>'
    f'<testcase name="formatsKey" classname="{cls}"><failure>boom</failure></testcase>'
    '</testsuite>')
print(f"[ERROR] Tests run: 2, Failures: 2 <<< FAILURE! -- in {cls}")
if "-Dmaven.test.failure.ignore=true" not in os.environ.get("MAVEN_OPTS", ""):
    print("[INFO] BUILD FAILURE"); sys.exit(1)
print("[INFO] BUILD SUCCESS")
'''


def test_build_gate_fails_on_a_repaired_test_that_still_fails(tmp_path, monkeypatch):
    import validation
    for v in ("MAVEN_OPTS", "MAVEN_ARGS", "HARNESS_RUNS_BUILD"):
        monkeypatch.delenv(v, raising=False)
    root = _repo()
    script = tmp_path / "fake.py"
    script.write_text(FAKE_BUILD)
    (root / ".harness" / "config.yaml").write_text(
        f'test_command: "{sys.executable} {script} {root}"\nmin_coverage: 0\n'
        'pre_validation_command: ""\n')
    run = _ut_run([ID_MAP, ID_KEY])
    vr = validation.run_validation(root, root / ".harness", log=quiet,
                                   known_failing=BL.gate_known_failing(run))
    assert vr.passed                                   # nothing repaired: both ignored
    run.unit_test["repaired"] = [ID_MAP]
    vr = validation.run_validation(root, root / ".harness", log=quiet,
                                   known_failing=BL.gate_known_failing(run))
    assert not vr.passed and "mapsName" in vr.output_tail
