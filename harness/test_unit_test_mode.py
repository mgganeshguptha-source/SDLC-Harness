"""
test_unit_test_mode.py — the unit-test run mode (unit_test_mode.py).

No SDK, no Maven, no network. Covers: the phase spine, settings, scope
resolution (repo / packages / classes / paths, excludes), per-file coverage
from JaCoCo and Istanbul reports, batch selection, config overrides (every
loopback goes to unit_testing), and the state machine's loopbacks in this mode:
coverage below target, red tests and review findings all go back to
unit_testing — never to a phase that could change production code.

Run:  python -m pytest test_unit_test_mode.py -v
"""
import json
import subprocess
import tempfile
from pathlib import Path

import unit_test_mode as UT
from config import HarnessConfig
from contracts import ExitCode
from phases import MODE_UNIT_TEST, normalize_mode, phases_for, next_phase
from state import RunState
from state_machine import StateMachine


# ------------------------------------------------------------------ helpers
def _repo(files: dict, config: str = "") -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / ".harness").mkdir(exist_ok=True)
    (root / ".harness" / "config.yaml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


JAVA = {
    "src/main/java/com/x/claims/ClaimService.java": "class ClaimService {}",
    "src/main/java/com/x/claims/ClaimMapper.java": "class ClaimMapper {}",
    "src/main/java/com/x/claims/dto/ClaimDto.java": "class ClaimDto {}",
    "src/main/java/com/x/members/MemberService.java": "class MemberService {}",
    "src/main/java/com/x/config/WebConfig.java": "class WebConfig {}",
    "src/main/java/com/x/App.java": "class App {}",
    "src/main/java/com/x/DemoApplication.java": "class DemoApplication {}",
    "src/test/java/com/x/claims/ClaimServiceTest.java": "class ClaimServiceTest {}",
    "src/main/resources/application.yml": "a: 1",
}


def _cfg(root: Path) -> HarnessConfig:
    return HarnessConfig.load(root / ".harness", repo_root=root)


def _run(**ut) -> RunState:
    r = RunState(feature_id="UT-1", story="add tests", current_phase="unit_testing",
                 mode="unit-test")
    r.unit_test = dict(ut)
    return r


# ------------------------------------------------------------------ spine
def test_unit_test_spine_and_mode_names():
    assert [p.id for p in phases_for("unit-test")] == ["unit_testing", "code_review", "raise_pr"]
    assert normalize_mode("unit_test") == MODE_UNIT_TEST
    assert normalize_mode("UNITTEST") == MODE_UNIT_TEST
    assert next_phase("unit_testing", "unit-test").id == "code_review"
    assert next_phase("raise_pr", "unit-test") is None
    # build mode untouched
    assert [p.id for p in phases_for("build")][0] == "repo_instructions"


def test_unit_testing_phase_still_writes_tests_only():
    ut = phases_for("unit-test")[0]
    assert "src/main/**" not in ut.allowed_writes
    assert "src/test/**" in ut.allowed_writes
    assert ut.validate_after is True


# ------------------------------------------------------------------ settings
def test_settings_defaults_and_fraction():
    root = _repo({}, "unit_test:\n  min_coverage: 0.8\n")
    s = UT.settings(_cfg(root))
    assert s.min_coverage == 80.0 and s.scope == "repo" and s.max_classes_per_run == 5
    root2 = _repo({}, "")
    assert UT.settings(_cfg(root2)).min_coverage == 90.0


def test_settings_problems():
    root = _repo({}, "unit_test:\n  scope: packages\n  coverage_metric: lines\n")
    bad = UT.problems(_cfg(root), UT.settings(_cfg(root)))
    assert any("packages is empty" in b for b in bad)
    assert any("coverage_metric" in b for b in bad)


# ------------------------------------------------------------------ scope
def test_repo_scope_applies_default_excludes():
    root = _repo(JAVA)
    files = UT.candidates(root, _cfg(root))
    assert "src/main/java/com/x/claims/ClaimService.java" in files
    assert "src/main/java/com/x/App.java" in files
    for gone in ("dto/ClaimDto.java", "WebConfig.java", "DemoApplication.java",
                 "ClaimServiceTest.java", "application.yml"):
        assert not any(f.endswith(gone) for f in files), gone


def test_requested_packages_classes_and_paths():
    root = _repo(JAVA)
    cfg = _cfg(root)
    f, un, src = UT.resolve(root, cfg, "com.x.claims")
    assert f == ["src/main/java/com/x/claims/ClaimMapper.java",
                 "src/main/java/com/x/claims/ClaimService.java"] and not un
    assert src == "workflow input test_targets"
    f, _, _ = UT.resolve(root, cfg, "MemberService, com.x.claims.ClaimMapper")
    assert f == ["src/main/java/com/x/claims/ClaimMapper.java",
                 "src/main/java/com/x/members/MemberService.java"]
    f, _, _ = UT.resolve(root, cfg, "src/main/java/com/x/members")
    assert f == ["src/main/java/com/x/members/MemberService.java"]
    f, un, _ = UT.resolve(root, cfg, "com.x.nothing")
    assert f == [] and un == ["com.x.nothing"]


def test_config_scope_classes_and_user_exclude():
    cfg_text = ("unit_test:\n  scope: classes\n  classes: [ClaimService, ClaimMapper]\n"
                "  exclude: ['**/*Mapper.java']\n")
    root = _repo(JAVA, cfg_text)
    f, _, src = UT.resolve(root, _cfg(root))
    assert f == ["src/main/java/com/x/claims/ClaimService.java"]
    assert src == "unit_test.classes"


def test_init_problem_runs_build_false_and_no_match(monkeypatch):
    root = _repo(JAVA, "runs_build: false\n")
    problem, _, _ = UT.init_problem(root, root / ".harness")
    assert "runs_build" in problem
    root2 = _repo(JAVA)
    problem, files, _ = UT.init_problem(root2, root2 / ".harness", "com.y.none")
    assert "matched no production file" in problem and not files
    problem, files, _ = UT.init_problem(root2, root2 / ".harness", "")
    assert problem == "" and len(files) == 4


def test_frontend_candidates_and_folder_target():
    files = {
        "src/app/claims/claim.service.ts": "x", "src/app/claims/claim.service.spec.ts": "x",
        "src/app/claims/claims.module.ts": "x", "src/app/claims/claim.model.ts": "x",
        "src/app/claims/claim-list.component.ts": "x",
        "src/app/claims/claim-list.component.html": "x",
        "src/app/members/member.service.ts": "x", "src/main.ts": "x",
    }
    root = _repo(files, "repo_stacks: [angular-frontend]\ncoverage_format: istanbul\n")
    cfg = _cfg(root)
    assert UT.candidates(root, cfg) == ["src/app/claims/claim-list.component.ts",
                                        "src/app/claims/claim.service.ts",
                                        "src/app/members/member.service.ts"]
    f, _, _ = UT.resolve(root, cfg, "src/app/claims")
    assert f == ["src/app/claims/claim-list.component.ts", "src/app/claims/claim.service.ts"]
    f, _, _ = UT.resolve(root, cfg, "member.service")
    assert f == ["src/app/members/member.service.ts"]


# ------------------------------------------------------------------ coverage
_CSV = ("GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED,BRANCH_MISSED,"
        "BRANCH_COVERED,LINE_MISSED,LINE_COVERED,COMPLEXITY_MISSED,COMPLEXITY_COVERED,"
        "METHOD_MISSED,METHOD_COVERED\n"
        "g,com.x.claims,ClaimService,0,0,0,0,8,2,0,0,0,0\n"
        "g,com.x.claims,ClaimService.Inner,0,0,0,0,0,0,0,0,0,0\n"
        "g,com.x.claims,ClaimMapper,0,0,0,0,1,9,0,0,0,0\n"
        "g,com.x.members,MemberService,0,0,0,0,10,0,0,0,0,0\n"
        "g,com.x,App,0,0,0,0,0,0,0,0,0,0\n")


def test_measure_jacoco_and_select_batch():
    root = _repo(dict(JAVA, **{"target/site/jacoco/jacoco.csv": _CSV}))
    cfg = _cfg(root)
    files = UT.candidates(root, cfg)
    found, pf = UT.measure(root, cfg, files)
    assert found
    assert pf["src/main/java/com/x/claims/ClaimService.java"]["pct"] == 20.0
    assert pf["src/main/java/com/x/claims/ClaimMapper.java"]["pct"] == 90.0
    assert pf["src/main/java/com/x/members/MemberService.java"]["pct"] == 0.0
    assert pf["src/main/java/com/x/App.java"]["pct"] == 100.0       # nothing to cover
    batch, rem, skipped = UT.select_batch(pf, files, 90.0, 1, found)
    assert batch == ["src/main/java/com/x/members/MemberService.java"]   # lowest first
    assert rem == ["src/main/java/com/x/claims/ClaimService.java"]
    assert skipped == []
    agg = UT.aggregate(pf, ["src/main/java/com/x/claims/ClaimService.java",
                            "src/main/java/com/x/members/MemberService.java"])
    assert agg == 10.0


def test_no_report_treats_everything_as_untested():
    root = _repo(JAVA)
    cfg = _cfg(root)
    files = UT.candidates(root, cfg)
    found, pf = UT.measure(root, cfg, files)
    assert not found
    batch, rem, skipped = UT.select_batch(pf, files, 90.0, 10, found)
    assert sorted(batch) == files and not skipped


def test_measure_istanbul():
    summary = {"total": {"lines": {"total": 10, "covered": 5}},
               "/w/src/app/claims/claim.service.ts": {"lines": {"total": 10, "covered": 5}}}
    files = {"src/app/claims/claim.service.ts": "x",
             "coverage/app/coverage-summary.json": json.dumps(summary)}
    root = _repo(files, "repo_stacks: [angular-frontend]\ncoverage_format: istanbul\n")
    found, pf = UT.measure(root, _cfg(root), ["src/app/claims/claim.service.ts"])
    assert found and pf["src/app/claims/claim.service.ts"]["pct"] == 50.0


# ------------------------------------------------------------------ overrides
def test_apply_overrides_routes_every_loop_to_unit_testing():
    root = _repo({}, "skip_phases: [unit_testing]\nmin_coverage: 0\nmax_phase_runs: 3\n"
                     "unit_test:\n  min_coverage: 85\n  coverage_metric: BRANCH\n")
    cfg = UT.apply_overrides(_cfg(root), _run())
    assert cfg.min_coverage == 85.0 and cfg.coverage_metric == "BRANCH"
    assert cfg.coverage_scope == "changed"
    assert cfg.skip_phases == []
    assert {cfg.validation_loopback_phase, cfg.test_failure_loopback_phase,
            cfg.coverage_loopback_phase, cfg.review_loopback_phase} == {"unit_testing"}
    assert cfg.max_phase_runs == 5
    # build mode untouched
    build = RunState(feature_id="B", story="s", current_phase="context")
    cfg2 = UT.apply_overrides(_cfg(root), build)
    assert cfg2.validation_loopback_phase == "coding" and cfg2.skip_phases == ["unit_testing"]


def test_coverage_targets_by_mode():
    r = _run(targets=["a.java"])
    r.changed_main_files = ["b.java"]
    assert UT.coverage_targets(r) == ["a.java"]
    b = RunState(feature_id="B", story="s", current_phase="context")
    b.changed_main_files = ["b.java"]
    assert UT.coverage_targets(b) == ["b.java"]


# ------------------------------------------------------------------ PR body
def test_pr_body_table_remaining_and_defects():
    root = _repo({})
    (root / ".harness" / "pr-body.md").write_text("Tests for claims.\n", encoding="utf-8")
    (root / ".harness" / "suspected-defects.md").write_text(
        "- ClaimService.java total(): ignores negative amounts\n", encoding="utf-8")
    r = _run(targets=["A.java"], coverage_before={"A.java": 10.0},
             coverage_after={"A.java": 95.0}, aggregate_before=10.0, aggregate_after=95.0,
             remaining=["B.java"], min_coverage=90.0, metric="LINE")
    assert UT.add_to_pr_body(root, r)
    assert UT.add_to_pr_body(root, r)          # idempotent: replaced, not appended
    body = (root / ".harness" / "pr-body.md").read_text(encoding="utf-8")
    assert body.count(UT.PR_MARKER) == 1
    assert "Tests for claims." in body
    assert "| `A.java` | 10.0% | 95.0% |" in body
    assert "`B.java`" in body and "ignores negative amounts" in body


def test_prompts_name_targets_and_forbid_production_edits():
    r = _run(targets=["src/main/java/com/x/A.java"], coverage_before={}, min_coverage=90.0,
             metric="LINE")
    t = UT.unit_testing_task(r, "/r", "/r/src/main/", "/r/src/test/", None)
    assert "src/main/java/com/x/A.java" in t and "MUST NOT create or edit" in t
    assert "suspected-defects.md" in t
    rv = UT.review_task(r, "/r", "DOSSIER", "/r/src/main/", "/r/src/test/")
    assert "(ASSERTION)" in rv and "DOSSIER" in rv and "Coverage is measured by the harness" in rv


# ------------------------------------------------------------------ state machine
class _Exec:
    """OK for every phase; writes review.md per a script of verdicts."""

    def __init__(self, root, verdicts=None):
        self.repo_root = root
        self.calls = []
        self.verdicts = list(verdicts or [])

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        if phase.id == "code_review":
            v = self.verdicts.pop(0) if self.verdicts else "PASS"
            body = ("VERDICT: PASS\n" if v == "PASS" else
                    "VERDICT: CHANGES_REQUESTED\n[ISSUE]: (ASSERTION) testFoo asserts nothing\n")
            (self.repo_root / ".harness" / "review.md").write_text(body, encoding="utf-8")
        return ExitCode.OK


class _VR:
    def __init__(self, passed, kind=None, tail=""):
        self.passed = passed
        self.summary = "FAKE"
        self.exit_code = 0 if passed else 1
        self.output_tail = tail
        self.failure_kind = kind
        self.coverage_pct = 40.0 if kind == "coverage" else None
        self.coverage_target = 90.0
        self.coverage_classes = []


def _validator(seq):
    seq = list(seq)

    def v(repo, hd, log):
        return seq.pop(0) if seq else _VR(True)
    return v


def _drive(sm, run, steps=40):
    for _ in range(steps):
        run = sm.run_until_pause(run)
        if run.status == "awaiting_approval":
            run = sm.resolve_gate(run, approved=True)
        elif run.status in ("done", "halted", "needs_input"):
            return run
    return run


def _ut_setup(monkeypatch, verdicts=None, validations=None, extra_cfg=""):
    monkeypatch.setenv("HARNESS_CONFIRM_NO_PHI", "true")
    # dependency_gate left ON: unit-test mode must not need the story's
    # contract line (it has no story).
    root = _repo(JAVA, "attachment_pii_scan: false\n" + extra_cfg)
    ex = _Exec(root, verdicts)
    sm = StateMachine(ex, root / ".harness", repo_root=root, log=lambda *a: None,
                      validator=_validator(validations or []))
    run = _run(targets=["src/main/java/com/x/claims/ClaimService.java"], min_coverage=90.0,
               metric="LINE")
    run.save(root / ".harness")
    return sm, ex, run


def test_happy_path(monkeypatch):
    sm, ex, run = _ut_setup(monkeypatch)
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.halt_reason, run.last_feedback)
    assert ex.calls == ["unit_testing", "code_review", "raise_pr"]


def test_coverage_below_target_loops_back_to_unit_testing(monkeypatch):
    sm, ex, run = _ut_setup(monkeypatch, validations=[_VR(False, "coverage", "COVERAGE 40%")])
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.last_feedback)
    assert ex.calls == ["unit_testing", "unit_testing", "code_review", "raise_pr"]
    assert run.coverage_attempts == 1
    assert "coding" not in ex.calls


def test_coverage_retries_exhausted_halts(monkeypatch):
    bad = [_VR(False, "coverage", "COVERAGE 40%") for _ in range(5)]
    sm, ex, run = _ut_setup(monkeypatch, validations=bad)
    run = _drive(sm, run)
    assert run.status == "halted" and run.halt_gate == "coverage"
    assert ex.calls.count("unit_testing") == 3          # first pass + 2 retries
    assert "code_review" not in ex.calls


def test_red_build_goes_to_unit_testing_never_coding(monkeypatch):
    red = _VR(False, "test", "[ERROR] Tests run: 3, Failures: 1\n"
                             "[ERROR] com.x.claims.ClaimServiceTest.testTotal:12 expected 1")
    sm, ex, run = _ut_setup(monkeypatch, validations=[red, red])
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.last_feedback)
    assert ex.calls[:3] == ["unit_testing", "unit_testing", "unit_testing"]
    assert "coding" not in ex.calls
    assert run.contract_snapshot == {} and run.fix_loop == ""


def test_review_findings_loop_to_unit_testing(monkeypatch):
    sm, ex, run = _ut_setup(monkeypatch, verdicts=["CHANGES", "PASS"])
    run = _drive(sm, run)
    assert run.status == "done", (run.status, run.last_feedback)
    assert ex.calls == ["unit_testing", "code_review", "unit_testing", "code_review", "raise_pr"]


def test_skip_phases_ignored_in_unit_test_mode(monkeypatch):
    sm, ex, run = _ut_setup(monkeypatch, extra_cfg="skip_phases: [unit_testing]\n")
    run = _drive(sm, run)
    assert run.status == "done"
    assert ex.calls[0] == "unit_testing"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
