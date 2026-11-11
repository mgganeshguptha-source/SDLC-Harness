"""PC-103 (2026-11-02): the PR body said the build was not run although the
harness gate had passed it, and the upstream OpenAPI attachment under
stories/ was published as this repo's own contract."""
from pathlib import Path

import sdk_runner
from handoff import detect_api_specs

SPEC = "openapi: 3.0.1\ninfo: {title: t, version: '1'}\npaths: {}\n"


def test_story_attachments_are_not_this_repos_spec(tmp_path: Path):
    (tmp_path / "stories/PC-103").mkdir(parents=True)
    (tmp_path / "stories/PC-103/openapi.yml").write_text(SPEC)
    (tmp_path / "src/main/resources").mkdir(parents=True)
    (tmp_path / "src/main/resources/openapi.yml").write_text(SPEC)
    assert detect_api_specs(tmp_path) == ["src/main/resources/openapi.yml"]


def test_pr_and_docs_told_build_passed(monkeypatch):
    src = Path(sdk_runner.__file__).read_text(encoding="utf-8")
    on = src[src.index("if harness_runs_build(repo_root / \".harness\"):"):src.index("    else:\n        build_status_validation")]
    assert "build_status_pr = (" in on and "PASSED" in on
    docs = src[src.index('"documentation": ('):src.index('"raise_pr": (')]
    assert "{build_status_pr}" in docs
