"""
test_handoff.py — the contract hand-off between stories (handoff.py). No SDK.

Run:  python -m pytest test_handoff.py -v
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import attachments as AT
import halt_gates as HG
import handoff as HO
from boundaries import is_write_allowed
from config import HarnessConfig
from contracts import ExitCode
from phases import PHASES, phase_by_id
from state import RunState
from state_machine import StateMachine

LINE = "**Requires a new or changed contract from:**"
SPEC = """openapi: 3.0.3
info:
  title: MEX Coverage Service
paths:
  /v1/personId/{personId}/visitSummary:
    get:
      responses:
        '200':
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/VisitSummaryResponse'
        '400':
          description: Bad Request
components:
  schemas:
    ApiErrorResponse:
      type: object
      properties:
        status: {type: string, enum: [SUCCESS, FAILED]}
        timestamp: {type: string, format: date-time}
    VisitSummaryResponse:
      type: object
      allOf:
        - $ref: '#/components/schemas/ApiErrorResponse'
        - description: SystemMessage
      properties:
        payload:
          type: object
          properties:
            contract:
              type: array
              items:
                type: object
                properties:
                  coverageKey: {type: string, example: 'REAL-VALUE-123'}
                  maximum: {type: integer, format: int64}
                  paid: {type: float}
                  unlimited: {type: boolean}
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for v in ("GITHUB_ACTIONS", "HARNESS_CONFIRM_NO_PHI", "HARNESS_BASE_BRANCH"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/dx_this-repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")


def _repo(tmp: Path, config: str = "") -> Path:
    (tmp / ".harness").mkdir(parents=True, exist_ok=True)
    if config:
        (tmp / ".harness" / "config.yaml").write_text(config, encoding="utf-8")
    return tmp


def _cfg(repo):
    return HarnessConfig.load(repo / ".harness", repo_root=repo)


def _att(repo, feature="F-2"):
    d = repo / "stories" / feature
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def _gitrepo(repo):
    _git(repo, "init", "-q", "-b", "story")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


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
    assert HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} None").ok


def test_gate_named_repo_without_files_halts(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "design.png").write_bytes(b"x")       # not a contract file
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_mex-coverage-service")
    assert not res.ok and res.gate == HG.CONTRACT_MISSING
    assert "OpenAPI file" in res.problems[0] and "src/app/store/" in res.problems[0]


def test_gate_openapi_file_tied_by_title(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "spec.yml").write_text(SPEC, encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_mex-coverage-service")
    assert res.ok and res.warnings == []
    assert res.contracts[0]["repo"] == "dx_mex-coverage-service"


def test_gate_openapi_file_tied_by_name_no_subfolder(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo) / "mex-coverage-service.yml").write_text("openapi: 3.0.3\n", encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_mex-coverage-service")
    assert res.ok and res.warnings == []


def test_gate_single_repo_takes_untied_files(tmp_path):
    # One upstream repo named: files copied as they are (no repo in the name)
    # are that repo's contract — no warning.
    repo = _repo(tmp_path)
    d = _att(repo) / "store" / "selectors"
    d.mkdir(parents=True)
    (d / "visitsummary.selectors.ts").write_text("export const x = 1;", encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_mpa_responsive")
    assert res.ok and not res.warnings
    assert all(c["repo"] == "dx_mpa_responsive" for c in res.contracts)


def test_gate_several_repos_untied_warns_only(tmp_path):
    repo = _repo(tmp_path)
    d = _att(repo) / "store" / "selectors"
    d.mkdir(parents=True)
    (d / "visitsummary.selectors.ts").write_text("export const x = 1;", encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2",
                                f"{LINE} dx_mpa_responsive, dx_other_repo")
    assert res.ok and any("could be tied" in w for w in res.warnings)


def test_gate_harness_summary_wrong_repo_halts(tmp_path):
    repo = _repo(tmp_path)
    hdr = HO.make_header({"repo": "dx_other", "feature_id": "F-1"})
    (_att(repo) / "contract-dx_other.md").write_text(hdr + "# Contract\n", encoding="utf-8")
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", f"{LINE} dx_up")
    assert not res.ok and "wrong contract attached" in res.problems[0]


def test_gate_story_section_accepted(tmp_path):
    repo = _repo(tmp_path)
    story = f"{LINE} dx_up\n\n## Upstream contract: dx_up\nGET /x returns {{}}\n"
    res = HO.check_dependencies(repo, _cfg(repo), "F-2", story)
    assert res.ok and res.warnings == []


# ------------------------------------------------------------------ samples
def test_openapi_samples_from_schema_only():
    import yaml
    s = HO.openapi_samples(yaml.safe_load(SPEC))
    op = s["GET /v1/personId/{personId}/visitSummary"]
    ok = op["responses"]["200"]
    assert ok["status"] == "SUCCESS" and ok["timestamp"] == "2026-01-01T00:00:00Z"
    item = ok["payload"]["contract"][0]
    assert item == {"coverageKey": "sample-coverageKey", "maximum": 1, "paid": 1.0,
                    "unlimited": False}
    assert "REAL-VALUE-123" not in json.dumps(s)            # examples ignored
    assert op["responses"]["400"].startswith("(no body schema")


def test_attached_openapi_gets_mocked_samples(tmp_path):
    repo = _repo(tmp_path)
    (_att(repo, "F-2") / "mex-coverage-service.yml").write_text(SPEC, encoding="utf-8")
    res = AT.prepare(repo, _cfg(repo), "F-2", f"{LINE} dx_mex-coverage-service")
    assert res.ok, res.problems
    names = [i["name"] for i in res.items]
    assert names == ["mex-coverage-service.yml", "mex-coverage-service.yml.mock-samples.json"]
    sample = next(i for i in res.items if i["converted_from"] == "openapi")
    assert "sample-coverageKey" in Path(sample["path"]).read_text()
    att = AT.sdk_attachments(AT.load_manifest(repo))
    assert "mocked samples generated from the schema" in att[1]["displayName"]


def test_store_files_with_same_names_stay_distinct(tmp_path):
    repo = _repo(tmp_path)
    for sub in ("actions", "selectors"):
        d = _att(repo) / "store" / sub
        d.mkdir(parents=True)
        (d / "index.ts").write_text(f"// {sub}", encoding="utf-8")
    res = AT.prepare(repo, _cfg(repo), "F-2", "s")
    assert sorted(i["name"] for i in res.items) == ["store/actions/index.ts", "store/selectors/index.ts"]


# ------------------------------------------------------------------ OpenAPI write
def _multimodule(repo):
    (repo / "pom.xml").write_text("<project><modules><module>svc-app</module>"
                                  "<module>svc-openapi-code</module></modules></project>")
    r = repo / "svc-openapi-code/src/main/resources"
    r.mkdir(parents=True)
    (r / "svc.yml").write_text(SPEC, encoding="utf-8")
    (repo / "svc-app/src/main/java/a").mkdir(parents=True)
    (repo / "svc-app/src/test/resources").mkdir(parents=True)
    (repo / "svc-app/src/test/resources/old.yml").write_text("openapi: 3.0.0\n")  # ignored


def test_detect_api_specs_skips_tests(tmp_path):
    repo = _repo(tmp_path)
    _multimodule(repo)
    assert HO.detect_api_specs(repo) == ["svc-openapi-code/src/main/resources/svc.yml"]


def test_coding_may_edit_openapi_file_only(tmp_path):
    repo = _repo(tmp_path)
    _multimodule(repo)
    cfg = _cfg(repo)
    ph = phase_by_id("coding")
    a, e = cfg.phase_writes(ph), cfg.phase_exclude(ph)
    ok = lambda p: is_write_allowed(p, a, str(repo), e)  # noqa: E731
    assert ok("svc-openapi-code/src/main/resources/svc.yml")
    assert not ok("svc-openapi-code/src/main/java/Generated.java")
    assert ok("svc-app/src/main/java/a/X.java")
    ut = phase_by_id("unit_testing")
    assert not is_write_allowed("svc-openapi-code/src/main/resources/svc.yml",
                                cfg.phase_writes(ut), str(repo), cfg.phase_exclude(ut))


def test_api_spec_files_config_overrides(tmp_path):
    repo = _repo(tmp_path, "api_spec_files: []\n")
    _multimodule(repo)
    cfg = _cfg(repo)
    assert not is_write_allowed("svc-openapi-code/src/main/resources/svc.yml",
                                cfg.phase_writes(phase_by_id("coding")), str(repo),
                                cfg.phase_exclude(phase_by_id("coding")))


def test_swagger_gap():
    specs = ["m/src/main/resources/svc.yml"]
    assert HO.swagger_gap(["a/VisitSummaryResponse.java"], specs) == ["a/VisitSummaryResponse.java"]
    assert HO.swagger_gap(["a/VisitSummaryResponse.java", specs[0]], specs) == []
    assert HO.swagger_gap(["a/Service.java"], specs) == []
    assert HO.swagger_gap(["a/VisitSummaryResponse.java"], []) == []


# ------------------------------------------------------------------ publishing
def test_finalize_backend_publishes_openapi(tmp_path):
    repo = _repo(tmp_path)
    _multimodule(repo)
    _gitrepo(repo)
    run = RunState(feature_id="F-1", story="s", current_phase="documentation")
    logs = []
    info = HO.finalize(repo, _cfg(repo), run, ["svc-app/src/main/java/a/VisitSummaryResponse.java"],
                       log=logs.append)
    assert info["status"] == "published"
    assert info["files"] == ["svc-openapi-code/src/main/resources/svc.yml"]
    assert info["swagger_not_updated"] == ["svc-app/src/main/java/a/VisitSummaryResponse.java"]
    m = json.loads((repo / ".harness/contract/manifest.json").read_text())
    assert m["repo"] == "dx_this-repo" and m["feature_id"] == "F-1" and m["run_id"] == "777"
    assert (repo / ".harness/contract/files/svc-openapi-code/src/main/resources/svc.yml").is_file()


def test_finalize_angular_publishes_store_folder(tmp_path):
    repo = _repo(tmp_path, 'repo_stacks: ["angular-frontend"]\napi_spec_files: []\n')
    for f in ("state/visit.state.ts", "selectors/index.ts", "actions/index.ts",
              "selectors/index.spec.ts"):
        p = repo / "src/app/store" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("export {}", encoding="utf-8")
    (repo / "src/app/x").mkdir(parents=True)
    (repo / "src/app/x/x.component.ts").write_text("x")
    run = RunState(feature_id="F-2", story="s", current_phase="documentation")
    info = HO.finalize(repo, _cfg(repo), run, ["src/app/store/selectors/index.ts"], log=lambda *a: None)
    assert info["status"] == "published"
    assert info["files"] == ["src/app/store/actions/index.ts", "src/app/store/selectors/index.ts",
                             "src/app/store/state/visit.state.ts"]
    # no contract file changed -> nothing published
    run2 = RunState(feature_id="F-2", story="s", current_phase="documentation")
    assert HO.finalize(repo, _cfg(repo), run2, ["src/app/x/x.component.ts"],
                       log=lambda *a: None)["status"] == "none"


def test_finalize_skips_file_with_pii(tmp_path):
    repo = _repo(tmp_path, 'repo_stacks: ["angular-frontend"]\napi_spec_files: []\n')
    p = repo / "src/app/store/state/a.ts"
    p.parent.mkdir(parents=True)
    p.write_text("// call 313-555-0142", encoding="utf-8")
    run = RunState(feature_id="F-2", story="s", current_phase="documentation")
    info = HO.finalize(repo, _cfg(repo), run, [], log=lambda *a: None, force=True)
    assert info["status"] == "missing" and info["skipped"][0]["file"] == "src/app/store/state/a.ts"
    assert "555-0142" not in json.dumps(info)


def test_summary_is_stamped(tmp_path):
    repo = _repo(tmp_path, 'repo_stacks: ["angular-frontend"]\napi_spec_files: []\n')
    p = repo / "src/app/store/a.ts"
    p.parent.mkdir(parents=True)
    p.write_text("export {}")
    md = HO.summary_path(repo)
    md.parent.mkdir(parents=True)
    md.write_text("# Contract: store\n")
    run = RunState(feature_id="F-2", story="s", current_phase="documentation")
    info = HO.finalize(repo, _cfg(repo), run, ["src/app/store/a.ts"], log=lambda *a: None)
    assert info["summary"] == md.name
    assert HO.parse_header(md.read_text())["repo"] == "dx_this-repo"


def test_contract_only_mode_end_to_end(tmp_path, monkeypatch):
    """No model: the contract command copies the OpenAPI file and collect-audit
    places it under contracts/<feature>/."""
    import run as R
    repo = _repo(tmp_path)
    _multimodule(repo)
    (repo / "stories").mkdir()
    (repo / "stories/F-1.md").write_text(f"Title\n{LINE} None\n")
    _gitrepo(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/story", "HEAD")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "svc-app/src/main/java/a/VisitSummaryResponse.java").write_text("class V{}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    monkeypatch.setattr(sys, "argv", ["run.py", "contract", "--repo", str(repo),
                                      "--feature", "F-1", "--base", "story"])
    R.main()
    monkeypatch.setattr(sys, "argv", ["run.py", "collect-audit", "--repo", str(repo)])
    R.main()
    out = repo / "contracts/F-1"
    assert (out / "files/svc-openapi-code/src/main/resources/svc.yml").is_file()
    assert json.loads((out / "manifest.json").read_text())["files"] == [
        "svc-openapi-code/src/main/resources/svc.yml"]
    assert list((repo / "audit/F-1").glob("*/contract/manifest.json"))


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
    run = sm.run_until_pause(RunState(feature_id="F-2", story="Title: no line",
                                      current_phase=PHASES[0].id))
    assert run.status == "halted" and run.halt_gate == HG.DEPENDENCIES_MISSING
    assert ex.calls == []


def test_machine_passes_with_none(tmp_path):
    repo = _repo(tmp_path)
    ex = _Ex()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = sm.step(RunState(feature_id="F-2", story=f"Title\n{LINE} None",
                           current_phase=PHASES[0].id))
    assert ex.calls == [PHASES[0].id] and run.dependencies["line"] == "none"


def test_gate_can_be_switched_off(tmp_path):
    repo = _repo(tmp_path, "dependency_gate: false\n")
    ex = _Ex()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    sm.step(RunState(feature_id="F-2", story="no line", current_phase=PHASES[0].id))
    assert ex.calls == [PHASES[0].id]


def test_is_excluded_negation():
    from boundaries import is_excluded
    ex = ["mod/**", "!mod/src/main/resources/a.yml"]
    assert not is_excluded("mod/src/main/resources/a.yml", ex)
    assert is_excluded("mod/src/main/java/A.java", ex)


# ------------------------------------------------- loose label (OP018-6418)
@pytest.mark.parametrize("story,status,repos", [
    ("**Requires new or changed contract from:** BCBSM/dx_mex", "repos", ["BCBSM/dx_mex"]),
    ("- Requires a changed contarct from: None", "none", []),
    ("## Requires New Or Changed Contract From: dx_a, dx_b", "repos", ["dx_a", "dx_b"]),
    ("**Contracts from:** None", "none", []),
    ("**Requires new or changed contract from:** <upstream repo>", "unfilled", []),
])
def test_loose_label_is_read(story, status, repos):
    r = HO.parse_requirement(story)
    assert (r.status, r.repos) == (status, repos)
    assert r.label


@pytest.mark.parametrize("story", [
    "**Dependencies:** Requires Story 1 (mex) contract from the backend first.",
    "**Upstream contract:** dx_a",
    "**Contact details from:** someone",
])
def test_other_lines_are_not_read_as_the_label(story):
    assert HO.parse_requirement(story).status == "missing"


def test_prose_label_with_a_sentence_value_still_halts():
    # Read as the label, but "the backend team" is not a repo name.
    assert HO.parse_requirement(
        "This story needs a contract from: the backend team").status == "unfilled"


def test_exact_label_wins_and_has_no_warning(tmp_path):
    story = f"**Contracts from:** dx_x\n{LINE} None\n"
    r = HO.parse_requirement(story)
    assert (r.status, r.label) == ("none", "")


def test_loose_label_passes_the_gate_with_a_warning(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-1",
                                "**Requires new or changed contract from:** None")
    assert res.ok
    assert any("Requires new or changed contract from" in w for w in res.warnings)


def test_near_miss_is_quoted_in_the_halt(tmp_path):
    repo = _repo(tmp_path)
    story = "**Upstream contract change needed:** dx_mex\n"
    res = HO.check_dependencies(repo, _cfg(repo), "F-1", story)
    assert not res.ok and res.gate == HG.DEPENDENCIES_MISSING
    assert any("did you mean" in p and "Upstream contract change needed" in p
               for p in res.problems)
    assert "did you mean" in HO.gate_message(res, "F-1")


def test_no_near_miss_for_unrelated_story(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-1", "**Title:** x\n**Owner:** y\n")
    assert not any("did you mean" in p for p in res.problems)


# ------------------------------------------- store at any depth (OP018-6418 Story 2)
def test_finalize_finds_a_nested_store_and_its_model(tmp_path):
    repo = _repo(tmp_path, 'repo_stacks: ["angular-frontend"]\ntarget_module: "mpa-web"\n'
                           'api_spec_files: []\n')
    app = repo / "mpa-web/src/app/mpa"
    files = {
        "store/actions/visitsummary.actions.ts": "export const a = 1;\n",
        "store/reducers/visitsummary.reducer.ts":
            "import { VisitSummary } from '../../shared/data/model/visitsummary';\n",
        "store/selectors/visitsummary.selectors.ts":
            "import * as fromVs from '../reducers/visitsummary.reducer';\n",
        "store/effects/visitsummary.effects.ts": "export class E {}\n",
        "store/effects/visitsummary.effects.spec.ts": "describe()\n",
        "store/README.md": "notes\n",
        "shared/data/model/visitsummary.ts": "export interface VisitSummary { status: string }\n",
        "shared/data/model/other.ts": "export interface Other {}\n",
    }
    for rel, text in files.items():
        p = app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    nm = repo / "mpa-web/node_modules/lib/store/x.ts"
    nm.parent.mkdir(parents=True)
    nm.write_text("x")
    run = RunState(feature_id="OP-1", story="s", current_phase="documentation")
    info = HO.finalize(repo, _cfg(repo), run,
                       ["mpa-web/src/app/mpa/store/effects/visitsummary.effects.ts"],
                       log=lambda *a: None)
    assert info["status"] == "published"
    assert info["files"] == [
        "mpa-web/src/app/mpa/shared/data/model/visitsummary.ts",
        "mpa-web/src/app/mpa/store/actions/visitsummary.actions.ts",
        "mpa-web/src/app/mpa/store/effects/visitsummary.effects.ts",
        "mpa-web/src/app/mpa/store/reducers/visitsummary.reducer.ts",
        "mpa-web/src/app/mpa/store/selectors/visitsummary.selectors.ts",
    ]


def test_contract_missing_message_names_store_and_model(tmp_path):
    repo = _repo(tmp_path)
    res = HO.check_dependencies(repo, _cfg(repo), "F-1", f"{LINE} dx_mpa_responsive\n")
    assert not res.ok and "model files it imports" in res.problems[0]
