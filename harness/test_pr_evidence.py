"""pr_evidence.py: the engine-written Evidence section of the PR body, and the
raise_pr prompt / skill wiring for the pr-description skill."""
import subprocess
from pathlib import Path

import pr_evidence as PE
import sdk_runner
from config import HarnessConfig
from state import RunState


def _git(root, *a):
    subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path, runs_build: bool = True) -> Path:
    root = tmp_path / "svc"
    (root / ".harness").mkdir(parents=True)
    (root / "src/main/java/a").mkdir(parents=True)
    (root / "src/test/java/a").mkdir(parents=True)
    (root / ".harness/config.yaml").write_text(
        f"runs_build: {'true' if runs_build else 'false'}\n")
    (root / "src/main/java/a/Svc.java").write_text("class Svc {}\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    # the run's change
    (root / "src/main/java/a/Svc.java").write_text("class Svc { int tier; }\n")
    (root / "src/test/java/a/SvcTest.java").write_text("class SvcTest {}\n")
    return root


def _run(**kw):
    return RunState(feature_id="PC-1", story="s", current_phase="raise_pr", **kw)


VALIDATION = """**VERDICT: INCONCLUSIVE**
### AC-2 — UNVERIFIABLE
no test exercises it
### AC-1 — MET
SvcTest#tier
"""

REVIEW = """VERDICT: PASS
[NOTE]: (STYLE) consider a constant for the tier name
"""


def test_evidence_section_from_gate_outputs(tmp_path):
    root = _repo(tmp_path)
    (root / ".harness/validation.md").write_text(VALIDATION)
    (root / ".harness/review.md").write_text(REVIEW)
    (root / ".harness/pr-body.md").write_text("# PC-1\n\n## Why\n\nBecause.\n")
    run = _run(review_attempts=1, validation_attempts=2)

    assert PE.add_to_pr_body(root, run) is True
    body = (root / ".harness/pr-body.md").read_text()

    assert body.startswith("# PC-1")                     # model part kept, first
    assert PE.PR_MARKER in body
    assert "written by the harness, not the AI" in body
    assert "built this exact code" in body               # runs_build true
    assert "| AC-1 | MET |" in body and "| AC-2 | UNVERIFIABLE |" in body
    assert body.index("| AC-1 |") < body.index("| AC-2 |")  # sorted by id
    assert "gate verdict `INCONCLUSIVE`" in body
    assert "`PASS` after 1 loop-back" in body
    assert "consider a constant" in body                 # [NOTE] surfaced on PASS
    assert "build (2)" in body
    assert "src/test/java/a/SvcTest.java" in body
    assert "never merges" in body


def test_evidence_is_added_once(tmp_path):
    root = _repo(tmp_path)
    (root / ".harness/pr-body.md").write_text("body\n")
    assert PE.add_to_pr_body(root, _run()) is True
    assert PE.add_to_pr_body(root, _run()) is False
    assert (root / ".harness/pr-body.md").read_text().count(PE.PR_MARKER) == 1


def test_no_build_says_how_to_verify(tmp_path):
    root = _repo(tmp_path, runs_build=False)
    (root / ".harness/pr-body.md").write_text("body\n")
    PE.add_to_pr_body(root, _run())
    body = (root / ".harness/pr-body.md").read_text()
    assert "NOT run by the harness" in body and "How to verify" in body
    assert "built this exact code" not in body


def test_missing_body_or_unit_test_mode_adds_nothing(tmp_path):
    root = _repo(tmp_path)
    assert PE.add_to_pr_body(root, _run()) is False     # no pr-body.md
    (root / ".harness/pr-body.md").write_text("body\n")
    assert PE.add_to_pr_body(root, _run(mode="unit-test")) is False


def test_prompt_block_lists_changes_and_diff(tmp_path):
    root = _repo(tmp_path)
    block = PE.prompt_block(root, _run())
    assert "production / other: src/main/java/a/Svc.java" in block
    assert "test: src/test/java/a/SvcTest.java" in block
    assert "New files" in block and "SvcTest.java" in block
    assert "--- GIT DIFF" in block and "int tier" in block


def test_prompt_block_large_diff_gives_stat_only(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(PE, "MAX_DIFF_CHARS", 10)
    block = PE.prompt_block(root, _run())
    assert "too large to include" in block and "--- GIT DIFF" not in block


def test_raise_pr_loads_pr_description_by_default():
    assert HarnessConfig().skills_for_phase("raise_pr") == ["pr-description"]


def test_raise_pr_prompt_uses_skill_and_forbids_result_claims():
    src = Path(sdk_runner.__file__).read_text(encoding="utf-8")
    pr = src[src.index('"raise_pr": (_UTp.raise_pr_task'):src.index("    # A phase added to phases.py")]
    assert "pr-description skill" in pr
    assert "Do NOT write build status" in pr
    assert "{build_status_pr}" not in pr.split("if _ut_mode else", 1)[1]


def test_pr_description_skill_skipped_in_unit_test_mode():
    src = Path(sdk_runner.__file__).read_text(encoding="utf-8")
    assert '_skip = ("pr-description",) if (_ut_mode and phase.id == "raise_pr")' in src
    assert "if skill_name in skip_skills:" in src
