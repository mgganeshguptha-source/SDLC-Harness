"""Frontend (Angular) support: configurable source/test paths (B4), skipping
unit_testing and the Istanbul coverage gate (B5). No SDK, no network, no npm."""
import json
import tempfile
from pathlib import Path

from boundaries import _matches, is_write_allowed
from config import HarnessConfig
from contracts import ExitCode
from phases import PHASES
import state_machine
import validation

PH = {p.id: p for p in PHASES}


def _cfg(tmp, text, poms=None):
    (tmp / ".harness").mkdir(exist_ok=True)
    (tmp / ".harness" / "config.yaml").write_text(text, encoding="utf-8")
    for rel, body in (poms or {}).items():
        f = tmp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return HarnessConfig.load(tmp / ".harness", repo_root=tmp)


def test_double_star_matches_zero_directories():
    assert _matches("src/app/a.spec.ts", "src/app/**/*.spec.ts")
    assert _matches("src/app/x/y/a.spec.ts", "src/app/**/*.spec.ts")
    assert not _matches("src/app/a.ts", "src/app/**/*.spec.ts")


def test_angular_coding_writes_source_not_specs(tmp_path):
    cfg = _cfg(tmp_path, 'repo_stacks: ["angular-frontend"]\n')
    a, e = cfg.phase_writes(PH["coding"]), cfg.phase_exclude(PH["coding"])
    ok = lambda f: is_write_allowed(f, a, repo_root=str(tmp_path), exclude_globs=e)
    assert ok("src/app/visit/visit.component.ts")
    assert ok("src/app/visit/visit.component.html")
    assert not ok("src/app/visit/visit.component.spec.ts")
    assert not ok("package.json")


def test_angular_unit_testing_writes_specs_only(tmp_path):
    cfg = _cfg(tmp_path, 'repo_stacks: ["angular-frontend"]\n')
    a, e = cfg.phase_writes(PH["unit_testing"]), cfg.phase_exclude(PH["unit_testing"])
    ok = lambda f: is_write_allowed(f, a, repo_root=str(tmp_path), exclude_globs=e)
    assert ok("src/app/visit/visit.component.spec.ts")
    assert ok("src/app/a.spec.ts")
    assert not ok("src/app/visit/visit.component.ts")


def test_backend_unchanged_single_and_multi_module(tmp_path):
    cfg = _cfg(tmp_path, 'repo_stacks: ["backend"]\n')
    assert cfg.phase_writes(PH["coding"]) == ("src/main/**", ".harness/**")
    assert cfg.phase_writes(PH["unit_testing"]) == ("src/test/**", ".harness/**")
    multi = tmp_path / "m"
    multi.mkdir()
    cfg2 = _cfg(multi, 'target_module: "svc-application"\n')
    assert cfg2.phase_writes(PH["coding"]) == ("svc-application/src/main/**", ".harness/**")
    assert cfg2.is_source_file("svc-application/src/main/java/a/B.java")
    assert not cfg2.is_source_file("svc-application/src/test/java/a/BTest.java")


def test_explicit_globs_override_preset(tmp_path):
    cfg = _cfg(tmp_path, 'repo_stacks: ["angular-frontend"]\n'
                         'source_globs: ["projects/lib/src/**"]\n'
                         'test_globs: ["projects/lib/src/**/*.spec.ts"]\n')
    assert cfg.phase_writes(PH["coding"])[0] == "projects/lib/src/**"
    assert cfg.is_test_file("projects/lib/src/x/a.spec.ts")


def test_skip_phases_only_allows_unit_testing(tmp_path):
    cfg = _cfg(tmp_path, 'skip_phases: ["unit_testing", "coding", "context"]\n')
    assert cfg.skip_phases == ["unit_testing"]


def test_coverage_format_auto(tmp_path):
    assert _cfg(tmp_path, 'repo_stacks: ["angular-frontend"]\n').resolved_coverage_format() == "istanbul"
    b = tmp_path / "b"; b.mkdir()
    assert _cfg(b, 'repo_stacks: ["backend"]\n').resolved_coverage_format() == "jacoco"


def test_typescript_error_routing():
    spec = "Error: src/app/a.component.spec.ts:12:5 - error TS2345: Argument of type"
    main = "Error: src/app/a.component.ts:12:5 - error TS2345: Argument of type"
    colour = "Error: \x1b[96msrc/app/a.spec.ts\x1b[0m:\x1b[93m7\x1b[0m:\x1b[93m5\x1b[0m - \x1b[91merror\x1b[0m\x1b[90m TS2322: x"
    assert state_machine._failure_is_in_tests(spec) is True
    assert state_machine._failure_is_in_tests(main) is False
    assert state_machine._failure_is_in_tests(colour) is True
    assert state_machine._failure_is_in_tests(spec + "\n" + main) is False


def test_npm_and_browser_failures_are_environment():
    assert validation.classify_environment_failure(1, "npm error code E404\n404 Not Found") == "dependency"
    assert validation.classify_environment_failure(1, "npm ERR! code ERESOLVE") == "dependency"
    assert validation.classify_environment_failure(
        1, "No binary for ChromeHeadless browser on your platform") == "toolchain"


def test_istanbul_changed_and_global(tmp_path):
    s = tmp_path / "coverage" / "x" / "coverage-summary.json"
    s.parent.mkdir(parents=True)
    s.write_text(json.dumps({
        "total": {"lines": {"total": 10, "covered": 6}},
        f"{tmp_path}/src/app/a.ts": {"lines": {"total": 4, "covered": 4}},
        f"{tmp_path}/src/app/b.ts": {"lines": {"total": 6, "covered": 2}},
    }))
    assert validation._find_istanbul_summary(tmp_path) == s
    pct, cov, miss, meas = validation._parse_istanbul(s, "LINE", ["src/app/b.ts"], "changed")
    assert round(pct, 1) == 33.3 and (cov, miss) == (2, 4)
    assert validation._parse_istanbul(s, "LINE", [], "global")[0] == 60.0
    assert validation._parse_istanbul(s, "LINE", ["src/app/x.html"], "changed")[0] == 100.0
    assert validation._parse_istanbul(s, "LINE", ["src/app/missing.ts"], "changed")[0] is None


def test_error_summary_keeps_typescript_and_karma_lines():
    out = ("Error: src/app/a.spec.ts:7:5 - error TS2322: Type x\n"
           "Chrome Headless (Linux) statusLabel null FAILED\n"
           "[ERROR] To see the full stack trace of the errors")
    got = validation.maven_error_summary(out)
    assert "error TS2322" in got and "FAILED" in got and "full stack trace" not in got


# ---- skip_phases in the state machine ------------------------------------
def test_skipped_unit_testing_runs_its_gate_but_not_the_ai(tmp_path, monkeypatch):
    from test_state_machine import ScriptedExecutor, _sm, _new_run, _drive, _FakeVR
    hd = tmp_path / ".harness"
    hd.mkdir()
    (hd / "config.yaml").write_text('skip_phases: ["unit_testing"]\n', encoding="utf-8")
    calls = []

    def validator(repo, h, log):
        calls.append("gate")
        return _FakeVR(True)

    import test_state_machine as tsm
    arts = dict(tsm._ARTIFACTS)
    arts[".harness/review.md"] = "VERDICT: PASS\nNo blocking issues.\n"
    monkeypatch.setattr(tsm, "_ARTIFACTS", arts)
    ex = ScriptedExecutor()
    sm = _sm(ex, hd, validator=validator)
    monkeypatch.setattr(state_machine, "harness_runs_build", lambda: True)
    run = _drive(sm, _new_run())
    assert "unit_testing" not in ex.calls       # no AI work
    assert calls                                 # the build gate still ran
    assert "unit_testing" in run.completed_phases
