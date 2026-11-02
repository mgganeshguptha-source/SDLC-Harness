"""Folders for files in the approved plan are made before coding/unit_testing
(petclinic-angular-demo PC-103, 2026-11-02: new src/app/store/visits/ could not
be created because shell is denied and the create tool needs the folder)."""
from pathlib import Path

from config import HarnessConfig
from phases import PHASES
from sdk_runner import ensure_planned_dirs

PLAN = """# Plan

## Impacted Files
| ID | Path | Role |
|----|------|------|
| F1 | `src/app/store/visits/visits.actions.ts` (new) | actions |
| F2 | `src/app/store/visits/visits-bridge.ts` | bridge |
| F3 | `src/app/app.component.ts` | dispatch |
| F4 | `package.json` | not allowed |
| F5 | `../outside/evil.ts` | escape |

## Steps
1. Do it.
"""


def _setup(tmp_path: Path, stacks: str = "frontend") -> HarnessConfig:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.yaml").write_text(f"repo_stacks: ['{stacks}']\n")
    (tmp_path / ".harness" / "prompt-steps.md").write_text(PLAN)
    (tmp_path / "src" / "app").mkdir(parents=True)
    return HarnessConfig.load(tmp_path / ".harness", repo_root=tmp_path)


def _phase(pid):
    return next(p for p in PHASES if p.id == pid)


def test_coding_creates_new_planned_folder(tmp_path: Path):
    cfg = _setup(tmp_path)
    ph = _phase("coding")
    made = ensure_planned_dirs(tmp_path, "coding", cfg.phase_writes(ph),
                               cfg.phase_exclude(ph), log=lambda *_: None)
    assert made == ["src/app/store/visits"]
    assert (tmp_path / "src/app/store/visits").is_dir()
    assert not (tmp_path.parent / "outside").exists()


def test_no_plan_no_folders(tmp_path: Path):
    cfg = _setup(tmp_path)
    (tmp_path / ".harness" / "prompt-steps.md").unlink()
    ph = _phase("coding")
    assert ensure_planned_dirs(tmp_path, "coding", cfg.phase_writes(ph),
                               cfg.phase_exclude(ph), log=lambda *_: None) == []


def test_unit_testing_mirrors_java_package(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.yaml").write_text("repo_stacks: ['backend']\n")
    (tmp_path / ".harness" / "prompt-steps.md").write_text(
        "## Impacted Files\n| ID | Path | Role |\n|---|---|---|\n"
        "| F1 | `src/main/java/org/x/status/VisitStatus.java` | new |\n")
    cfg = HarnessConfig.load(tmp_path / ".harness", repo_root=tmp_path)
    ph = _phase("unit_testing")
    made = ensure_planned_dirs(tmp_path, "unit_testing", cfg.phase_writes(ph),
                               cfg.phase_exclude(ph), log=lambda *_: None)
    assert "src/test/java/org/x/status" in made
    # unit_testing may not write main source, so its folder is not made here.
    assert "src/main/java/org/x/status" not in made
