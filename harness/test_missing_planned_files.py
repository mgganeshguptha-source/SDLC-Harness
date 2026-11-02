"""Planned files that coding never created are caught at the coding gate, not
three review loops later (petclinic-angular-demo PC-103, 2026-11-02)."""
from pathlib import Path

from contracts import ExitCode
from execution_record import missing_planned_files, read_plan_skips
from executor import PhaseExecutor
from state import RunState
from phases import PHASES

PLAN = """## Impacted Files
| ID | Path | Role |
|----|------|------|
| F1 | `src/app/store/visits/visits.actions.ts` (new) | actions |
| F2 | `src/app/store/visits/visits.selectors.ts` (new) | selectors |
| F3 | `src/app/app.component.ts` | dispatch |
| F4 | `src/app/old/legacy.ts` | delete this file |
| F5 | `package.json` | not coding's |

## Steps
1. Do it.
"""


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.yaml").write_text("repo_stacks: ['frontend']\n")
    (tmp_path / ".harness" / "prompt-steps.md").write_text(PLAN)
    (tmp_path / "src/app/store/visits").mkdir(parents=True)
    (tmp_path / "src/app/app.component.ts").write_text("x")
    return tmp_path


def _writable(p: str) -> bool:
    return p.startswith("src/app/")


def test_reports_only_missing_writable_non_deleted(tmp_path: Path):
    r = _repo(tmp_path)
    assert missing_planned_files(PLAN, r, _writable) == [
        "src/app/store/visits/visits.actions.ts",
        "src/app/store/visits/visits.selectors.ts",
    ]


def test_skip_needs_a_reason(tmp_path: Path):
    r = _repo(tmp_path)
    (r / ".harness/plan-skips.md").write_text(
        "SKIPPED: src/app/store/visits/visits.selectors.ts — folded into reducer\n"
        "SKIPPED: src/app/store/visits/visits.actions.ts\n")
    assert read_plan_skips(r) == {
        "src/app/store/visits/visits.selectors.ts": "folded into reducer"}


def _executor(r: Path) -> PhaseExecutor:
    return PhaseExecutor(runner=None, repo_root=r, harness_dir=r / ".harness",
                         log=lambda *_: None)


def _coding():
    return next(p for p in PHASES if p.id == "coding")


def test_sent_back_once_then_halts(tmp_path: Path):
    r = _repo(tmp_path)
    ex, run = _executor(r), RunState(feature_id="PC-103", story="s", current_phase="coding")
    plan = r / ".harness/prompt-steps.md"
    first = ex._check_planned_files(_coding(), run, plan, ["src/app/**"], None)
    assert first == ExitCode.REJECTED
    assert "visits.actions.ts" in run.last_feedback
    second = ex._check_planned_files(_coding(), run, plan, ["src/app/**"], None)
    assert second == ExitCode.ARTIFACT_MISSING
    assert "PLANNED FILES MISSING: HALTED" in run.last_feedback


def test_all_created_or_explained_passes(tmp_path: Path):
    r = _repo(tmp_path)
    (r / "src/app/store/visits/visits.actions.ts").write_text("x")
    (r / ".harness/plan-skips.md").write_text(
        "- SKIPPED: `src/app/store/visits/visits.selectors.ts` — not needed after review\n")
    ex, run = _executor(r), RunState(feature_id="PC-103", story="s", current_phase="coding")
    assert ex._check_planned_files(_coding(), run, r / ".harness/prompt-steps.md",
                                   ["src/app/**"], None) is None
