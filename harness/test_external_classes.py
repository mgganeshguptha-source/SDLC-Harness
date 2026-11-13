"""A plan that needs a class with no confirmed source halts before coding
(OP018-6434, 2026-11-04)."""
from pathlib import Path

from plan_check import check_external, check_placeholders, external_declaration
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


PLACEHOLDER_PLAN = """# Plan

## External Classes
None.

## Impacted Files
| ID | Path | Role |
|----|------|------|
| F1 | `app/src/main/java/com/x/webservice/<MPA-totals-SOAP-interface>.java` | new |
| F2 | app/src/main/java/com/x/service/Accum.java | returns List<Accum> |
"""


def test_only_placeholder_paths_are_flagged(tmp_path: Path):
    assert check_placeholders(_plan(tmp_path, PLACEHOLDER_PLAN)) == [
        "app/src/main/java/com/x/webservice/<MPA-totals-SOAP-interface>.java"]


def test_generics_outside_impacted_files_are_ignored(tmp_path: Path):
    text = "## Steps\nReturn List<Accum> from src/main/Foo.java\n\n## Impacted Files\n" \
           "| ID | Path | Role |\n|----|------|------|\n| F1 | src/main/Foo.java | edit |\n"
    assert check_placeholders(_plan(tmp_path, text)) == []


def test_placeholder_path_halts_before_coding(tmp_path: Path, monkeypatch):
    ex, run = _run_with_plan(tmp_path, monkeypatch, PLACEHOLDER_PLAN)
    assert run.status == "halted"
    assert "coding" not in ex.calls
    assert "<MPA-totals-SOAP-interface>" in run.last_feedback
    assert "fresh run" in run.last_feedback


# ------------------------------------- COPY_FROM_REFERENCE (OP018-6434, 2026-11-13)
COPY_PLAN = """# Plan

## External Classes
| Class or import | Source | Evidence |
|---|---|---|
| com.bcbsm.totals2_0.schema.* | COPY_FROM_REFERENCE | `BCBSM/dx_mpa@master:mpa-app/mpa-data/src/generatedSources/java/com/bcbsm/totals2_0/schema` |
| com.bcbsm.cs.service.totals2_0.TotalsService | **Copy from reference** | dx_mpa:mpa-app/mpa-data/src/generatedSources/java/com/bcbsm/cs/service/totals2_0 |
| com.x.Mapper | DEPENDENCY | app/pom.xml |

## Impacted Files
| ID | Path | Role |
"""


def test_copy_rows_are_flagged_with_their_evidence(tmp_path: Path):
    got = check_external(_plan(tmp_path, COPY_PLAN), log=lambda *a: None)
    assert [c.source for c in got] == ["COPY_FROM_REFERENCE", "COPY_FROM_REFERENCE"]


def test_copy_items_map_to_this_repos_source_root(tmp_path: Path):
    from plan_check import copy_items
    flagged = check_external(_plan(tmp_path, COPY_PLAN), log=lambda *a: None)
    refs = [{"repo": "BCBSM/dx_mpa", "branch": "release-11"}]
    items = copy_items(flagged, "mbp-coverage-service-application", refs)
    assert [(i.repo, i.branch, i.dest) for i in items] == [
        ("BCBSM/dx_mpa", "master",
         "mbp-coverage-service-application/src/main/java/com/bcbsm/totals2_0/schema"),
        ("dx_mpa", "release-11",       # branch from the reference repo the run read
         "mbp-coverage-service-application/src/main/java/com/bcbsm/cs/service/totals2_0"),
    ]


def test_copy_halt_message_gives_the_commands(tmp_path: Path):
    from blocked import halt_message
    flagged = check_external(_plan(tmp_path, COPY_PLAN), log=lambda *a: None)
    d = external_declaration(flagged, tmp_path, "mbp-coverage-service-application")
    msg = halt_message("OP018-6434", "prompt_steps", d)
    assert "UNCHANGED" in msg
    assert ('cp -r "$REF/mpa-app/mpa-data/src/generatedSources/java/com/bcbsm/totals2_0/schema" '
            "mbp-coverage-service-application/src/main/java/com/bcbsm/totals2_0/") in msg
    assert "mbp-coverage-service-application/src/main/java/com/bcbsm/cs/service/totals2_0" in d.file
    assert "pom.xml" not in d.file              # nothing for a build file here
    assert "resume      = true" in msg


def test_copy_and_unknown_together_name_both_fixes(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pom.xml").write_text("<project/>")
    text = COPY_PLAN.replace("| com.x.Mapper | DEPENDENCY |", "| com.x.Mapper | UNKNOWN |")
    flagged = check_external(_plan(tmp_path, text), log=lambda *a: None)
    d = external_declaration(flagged, tmp_path, "app")
    assert "app/pom.xml" in d.file and "app/src/main/java/com/bcbsm/totals2_0/schema" in d.file
    assert "copied in unchanged" in d.needs and "no confirmed source" in d.needs


def test_copy_row_without_a_usable_path_is_treated_as_unknown(tmp_path: Path):
    text = COPY_PLAN.replace("dx_mpa:mpa-app/mpa-data/src/generatedSources/java/com/bcbsm/cs/service/totals2_0",
                             "somewhere in MPA")
    flagged = check_external(_plan(tmp_path, text), log=lambda *a: None)
    d = external_declaration(flagged, tmp_path, "")
    assert "no confirmed source" in d.needs


def test_copy_plan_halts_before_coding(tmp_path: Path, monkeypatch):
    ex, run = _run_with_plan(tmp_path, monkeypatch, COPY_PLAN)
    assert run.status == "halted" and run.halt_gate == "manual_change_required"
    assert "coding" not in ex.calls
    assert 'cp -r "$REF/' in run.last_feedback
