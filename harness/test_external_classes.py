"""A plan that needs a class with no confirmed source halts before coding
(OP018-6434, 2026-11-04)."""
from pathlib import Path

from plan_check import check_external, external_declaration
from test_state_machine import _ARTIFACTS, ScriptedExecutor, _drive, _new_run, _sm

PLAN = """# Plan

## External Classes
| Class or import | Source | Evidence |
|-----------------|--------|----------|
| `com.x.GetAccumRequest` | UNKNOWN | repo source, pom.xml |
| com.x.Client | **New dependency** | pom.xml |
| com.y.Mapper | DEPENDENCY | app/pom.xml line 40 |
| com.y.Generated | GENERATED | src/main/wsdl/a.wsdl |
| com.y.Named | STORY | story |

## Impacted Files
| ID | Path | Role |
"""


def _plan(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "prompt-steps.md"
    f.write_text(text, encoding="utf-8")
    return f


def test_only_unknown_and_new_dependency_are_flagged(tmp_path: Path):
    got = check_external(_plan(tmp_path, PLAN), log=lambda *a: None)
    assert [(c.name, c.source) for c in got] == [
        ("com.x.GetAccumRequest", "UNKNOWN"), ("com.x.Client", "NEW_DEPENDENCY")]
    assert got[0].evidence == "repo source, pom.xml"


def test_table_stops_at_the_next_heading(tmp_path: Path):
    text = PLAN + "| F1 | UNKNOWN | not in the external table |\n"
    assert len(check_external(_plan(tmp_path, text), log=lambda *a: None)) == 2


def test_none_and_missing_section_do_not_halt(tmp_path: Path):
    assert check_external(_plan(tmp_path, "## External Classes\nNone.\n"),
                          log=lambda *a: None) == []
    logged = []
    assert check_external(_plan(tmp_path, "# Plan\n"), log=logged.append) == []
    assert "no External Classes section" in logged[0]


def test_declaration_names_the_classes_and_module_build_file(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pom.xml").write_text("<project/>")
    (tmp_path / "pom.xml").write_text("<project/>")
    flagged = check_external(_plan(tmp_path, PLAN), log=lambda *a: None)
    d = external_declaration(flagged, tmp_path, "app")
    assert "com.x.GetAccumRequest (UNKNOWN)" in d.missing
    assert "com.x.Client (NEW_DEPENDENCY)" in d.missing
    assert d.file == "app/pom.xml"
    assert external_declaration(flagged, tmp_path, "").file == "pom.xml"


def _run_with_plan(tmp_path: Path, monkeypatch, text: str):
    hd = tmp_path / ".harness"
    hd.mkdir()
    (hd / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setitem(_ARTIFACTS, ".harness/prompt-steps.md", text)
    ex = ScriptedExecutor()
    run = _drive(_sm(ex, hd), _new_run())
    return ex, run


def test_unknown_class_halts_before_coding(tmp_path: Path, monkeypatch):
    ex, run = _run_with_plan(tmp_path, monkeypatch, PLAN)
    assert run.status == "halted"
    assert run.halt_gate == "manual_change_required"
    assert run.current_phase == "prompt_steps"
    assert "coding" not in ex.calls
    assert "com.x.GetAccumRequest (UNKNOWN)" in run.last_feedback


def test_known_sources_reach_coding(tmp_path: Path, monkeypatch):
    text = PLAN.replace("UNKNOWN", "DEPENDENCY").replace("**New dependency**", "STORY")
    ex, _run = _run_with_plan(tmp_path, monkeypatch, text)
    assert "coding" in ex.calls
