"""Run naming, coverage evidence and resume dossiers (BCBSM pilot fixes)."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import state
import metrics
import sdk_runner


def test_run_label_uses_feature_actor_and_fixed_stamp(monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_STAMP", "20261022-122837EDT")
    monkeypatch.setenv("GITHUB_ACTOR", "760822_bcbsm")
    assert state.run_label("OP018-6418") == "OP018-6418-760822_bcbsm-20261022-122837EDT"


def test_run_label_cleans_unsafe_characters(monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_STAMP", "20261022-122837EDT")
    monkeypatch.setenv("GITHUB_ACTOR", "a b/c")
    assert state.run_label("X/1") == "X-1-a-b-c-20261022-122837EDT"


def test_metrics_file_named_by_run_label(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_STAMP", "20261022-122837EDT")
    monkeypatch.setenv("GITHUB_ACTOR", "dev1")
    p = metrics.write_record({"feature_id": "F-1", "run_id": "123"}, tmp_path, log=lambda *_: None)
    assert p.name == "F-1-dev1-20261022-122837EDT.json"
    assert p.parent.as_posix().endswith("metrics/2026/10")
    assert json.loads(p.read_text())["run_id"] == "123"


def test_run_label_fallback_is_us_eastern(monkeypatch):
    monkeypatch.delenv("HARNESS_RUN_STAMP", raising=False)
    monkeypatch.setenv("GITHUB_ACTOR", "dev1")
    lab = state.run_label("F-1")
    assert lab.startswith("F-1-dev1-") and lab[-3:] in ("EDT", "EST")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_unittest_dossier_falls_back_to_recorded_files_on_resume(tmp_path):
    # Resume case: the production change is already committed, so git diff HEAD is empty.
    f = tmp_path / "src/main/java/a/B.java"
    f.parent.mkdir(parents=True)
    f.write_text("class B {}")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "wip")
    assert sdk_runner._build_unittest_dossier(tmp_path, log=lambda *_: None) is None
    d = sdk_runner._build_unittest_dossier(tmp_path, log=lambda *_: None,
                                           changed_hint=["src/main/java/a/B.java"])
    assert d and "class B {}" in d
    r = sdk_runner._build_review_dossier(tmp_path, log=lambda *_: None,
                                         changed_hint=["src/main/java/a/B.java", "gone.java"])
    assert r and "class B {}" in r and "gone.java" not in r


def test_documentation_phase_creates_decisions_dir(tmp_path):
    import executor, phases
    from state import RunState
    (tmp_path / ".harness").mkdir()

    class Stop(Exception):
        pass

    class Runner:
        def run(self, *a, **k):
            raise Stop

    doc = next(p for p in phases.PHASES if p.id == "documentation")
    ex = executor.PhaseExecutor(Runner(), tmp_path, tmp_path / ".harness", log=lambda *_: None)
    run = RunState(feature_id="F-1", story="s\n**Requires a new or changed contract from:** None", current_phase="documentation")
    try:
        ex.run_phase(doc, run)
    except Stop:
        pass
    assert (tmp_path / "docs" / "decisions").is_dir()
