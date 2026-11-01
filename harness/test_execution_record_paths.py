"""Plan-table paths must match written files whatever Markdown the planner used
(petclinic-rest-api PC-101, 2026-11-01: a backticked, approved new file was
flagged as a SCOPE_VIOLATION)."""
from execution_record import (_approved_paths_from_plan, _plan_cell_path,
                              classify_scope_additions)

PLAN = """\
## Impacted Files
| ID | Path | Role |
|----|------|------|
| F1 | `src/main/java/org/x/model/VisitStatus.java` | new enum |
| F2 | src/main/java/org/x/model/Visit.java | add field |
| F3 | **`src/main/resources/openapi.yml`** | contract |
| F4 | src/main/java/org/x/web/VisitController.java (new) | controller |

## Steps
"""


def test_cell_decorations_are_stripped():
    assert _plan_cell_path("`src/main/a/B.java`") == "src/main/a/B.java"
    assert _plan_cell_path("**`src/main/a/B.java`**") == "src/main/a/B.java"
    assert _plan_cell_path("src/main/a/B.java (new)") == "src/main/a/B.java"
    assert _plan_cell_path("./src/main/a/B.java") == "src/main/a/B.java"


def test_approved_paths_parse_backticked_rows():
    assert _approved_paths_from_plan(PLAN) == {
        "src/main/java/org/x/model/VisitStatus.java",
        "src/main/java/org/x/model/Visit.java",
        "src/main/resources/openapi.yml",
        "src/main/java/org/x/web/VisitController.java",
    }


def test_planned_new_file_is_not_a_violation():
    approved = _approved_paths_from_plan(PLAN)
    written = ["src/main/java/org/x/model/VisitStatus.java",
               "src/main/java/org/x/model/Unplanned.java"]
    additions = [w for w in written if w not in approved]
    split = classify_scope_additions(additions, pre_existing=set())
    assert split["created"] == ["src/main/java/org/x/model/Unplanned.java"]
