"""
test_handoff.py — the contract hand-off between stories (handoff.py). No SDK.

Run:  python -m pytest test_handoff.py -v
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import halt_gates as HG
import handoff as HO
from config import HarnessConfig
from contracts import ExitCode
from phases import PHASES
from state import RunState
from state_machine import StateMachine

LINE = "**Requires a new or changed contract from:**"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("HARNESS_CONFIRM_NO_PHI", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/dx_this-repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")


def _repo(tmp: Path, config: str = "") -> Path:
    (tmp / ".harness").mkdir(parents=True, exist_ok=True)
    if config:
        (tmp / ".harness" / "config.yaml").write_text(config, encoding="utf-8")
    return tmp


def _cfg(repo):
    return HarnessConfig.load(repo / ".harness")


def _att(repo, feature="F-2"):
    d = repo / "stories" / feature
    d.mkdir(parents=True, exist_ok=True)
    return d


def _contract(repo_name="dx_up", feature="F-1"):
    return HO.make_header({"repo": repo_name, "feature_id": feature, "run_id": "1",
                           "commit": "abc", "boundary_files": ["src/x.ts"]}) + "\n# Contract\n"


# ------------------------------------------------------------------ the line
@pytest.mark.parametrize("story,status,repos", [
    (f"Title\n{LINE} None\n", "none", []),
    (f"{LINE} none.", "none", []),
    ("- **Requires a new or changed contract from**: dx_mpa_responsive", "repos", ["dx_mpa_responsive"]),
    (f"{LINE} `dx_a`, dx_b", "repos", ["dx_a", "dx_b"]),
    (f"{LINE} None | <upstream repo>", "unfilled", []),
    (f"{LINE} ", "unfilled", []),
    (f"{LINE} the mex service I think", "unfilled", []),
    ("**Dependencies:** None", "missing", []),
    ("", "missing", []),
])
def test_parse_requirement(story, status, repos):
    r = HO.parse_requirement(story)
    assert (r.status, r.repos) == (status, repos)


# ------------------------------------------------------------------ gate
def test_gate_missing_line_halts(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", "Title only")
    assert not res.ok and res.gate == HG.DEPENDENCIES_MISSING


def test_gate_none_passes(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} None")
    assert res.ok and res.requirement.status == "none"


def test_gate_named_repo_without_contract_halts(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_up")
    assert not res.ok and res.gate == HG.CONTRACT_MISSING
    assert "harness-metrics" in res.problems[0]


def test_gate_matching_contract_passes_and_reads_header(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "contract-dx_up.md").write_text(_contract(), encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_up")
    assert res.ok and res.warnings == []
    assert res.contracts[0]["feature_id"] == "F-1" and res.contracts[0]["commit"] == "abc"


def test_gate_wrong_contract_halts(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "contract-dx_up.md").write_text(_contract(repo_name="dx_other"), encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_up")
    assert not res.ok and "wrong contract attached" in res.problems[0]


def test_gate_misspelt_repo_halts(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "contract-dx_up.md").write_text(_contract(), encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_upp")
    assert not res.ok and res.gate == HG.CONTRACT_MISSING


def test_gate_handwritten_contract_warns(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "contract-dx_up.md").write_text("# my notes\n", encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_up")
    assert res.ok and "no engine header" in res.warnings[0]


def test_gate_story_section_accepted_with_warning(tmp_path):
    repo = _repo(tmp_path)
    story = f"{LINE} dx_up\n\n## Upstream contract: dx_up\nGET /x returns {{}}\n"
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", story)
    assert res.ok and "pasted into the story" in res.warnings[0]


# ------------------------------------------------------------------ boundary
def test_boundary_patterns_per_stack(tmp_path):
    be = SimpleNamespace(repo_stacks=["backend"], contract_files=[])
    fe = SimpleNamespace(repo_stacks=["angular-frontend"], contract_files=["src/app/shared/hub/**"])
    changed_be = ["src/main/java/a/VisitSummaryController.java",
                  "src/main/java/a/dto/VisitSummaryResponse.java",
                  "src/main/java/a/service/VisitService.java", "openapi.yaml"]
    assert HO.boundary_files(changed_be, be) == changed_be[:2] + ["openapi.yaml"]
    changed_fe = ["src/app/store/visitsummary.reducer.ts", "src/app/x/x.component.ts",
                  "src/app/shared/hub/feed.ts", "src/app/core/host-bridge.service.ts"]
    assert HO.boundary_files(changed_fe, fe) == [
        "src/app/store/visitsummary.reducer.ts", "src/app/shared/hub/feed.ts",
        "src/app/core/host-bridge.service.ts"]
    assert HO.boundary_files(["src/app/x/x.component.ts"], fe) == []


# ------------------------------------------------------------------ finalize
def test_finalize_stamps_header(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    md, js = HO.contract_paths(repo)
    md.parent.mkdir(parents=True)
    md.write_text("# Contract: visit summary API\n", encoding="utf-8")
    js.write_text(json.dumps({"success": {"status": "SUCCESS"}}), encoding="utf-8")
    run = RunState(feature_id="F-1", story="s", current_phase="documentation")
    info = HO.finalize(repo, _cfg(repo), run, ["src/a/VisitResponse.java"], log=lambda *a: None)
    assert info["status"] == "published" and run.contract_output["status"] == "published"
    assert md.name == "contract-dx_this-repo.md"
    hdr = HO.parse_header(md.read_text())
    assert hdr["repo"] == "dx_this-repo" and hdr["feature_id"] == "F-1" and hdr["run_id"] == "777"
    assert hdr["boundary_files"] == ["src/a/VisitResponse.java"]
    # re-finalizing does not stack headers
    HO.finalize(repo, _cfg(repo), run, ["src/a/VisitResponse.java"], log=lambda *a: None)
    assert md.read_text().count(HO.HEADER_START) == 1


def test_finalize_pii_blocks_publishing(tmp_path):
    repo = _repo(tmp_path)
    md, js = HO.contract_paths(repo)
    md.parent.mkdir(parents=True)
    md.write_text("# Contract\nexample member ssn 123-45-6789\n", encoding="utf-8")
    run = RunState(feature_id="F-1", story="s", current_phase="documentation")
    logs = []
    info = HO.finalize(repo, _cfg(repo), run, ["src/a/X.java"], log=logs.append)
    assert info["status"] == "blocked_pii" and not md.exists()
    assert "123-45" not in json.dumps(info) and "123-45" not in " ".join(logs)


def test_finalize_missing_and_none(tmp_path):
    repo = _repo(tmp_path)
    run = RunState(feature_id="F-1", story="s", current_phase="documentation")
    assert HO.finalize(repo, _cfg(repo), run, [], log=lambda *a: None)["status"] == "none"
    assert HO.finalize(repo, _cfg(repo), run, ["src/a/X.java"], log=lambda *a: None)["status"] == "missing"


def test_prompt_block_names_paths_and_mocked_rule(tmp_path):
    repo = _repo(tmp_path)
    blk = HO.prompt_block(repo, ["src/app/visitsummary.reducer.ts"])
    assert "contract-dx_this-repo.md" in blk and "MOCKED VALUES ONLY" in blk
    assert "visitsummary.reducer.ts" in blk
    assert HO.prompt_block(repo, []) == ""


# ------------------------------------------------------------------ machine
class _Ex:
    def __init__(self):
        self.calls = []

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        return ExitCode.OK


def test_machine_halts_on_missing_line_before_model_call(tmp_path):
    repo = _repo(tmp_path)
    ex = _Ex()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = RunState(feature_id="F-2", story="Title: no contract line", current_phase=PHASES[0].id)
    run = sm.run_until_pause(run)
    assert run.status == "halted" and run.halt_gate == HG.DEPENDENCIES_MISSING
    assert ex.calls == []


def test_machine_passes_with_none(tmp_path):
    repo = _repo(tmp_path)
    ex = _Ex()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = RunState(feature_id="F-2", story=f"Title\n{LINE} None", current_phase=PHASES[0].id)
    run = sm.step(run)
    assert ex.calls == [PHASES[0].id] and run.dependencies["line"] == "none"


def test_gate_can_be_switched_off(tmp_path):
    repo = _repo(tmp_path, "dependency_gate: false\n")
    ex = _Ex()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = RunState(feature_id="F-2", story="no line", current_phase=PHASES[0].id)
    sm.step(run)
    assert ex.calls == [PHASES[0].id]
