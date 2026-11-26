"""Per-phase timeout for the model session (config.timeout_for_phase)."""
from pathlib import Path

from config import HarnessConfig


def _cfg(tmp: Path, text: str) -> HarnessConfig:
    (tmp / ".harness").mkdir(exist_ok=True)
    (tmp / ".harness" / "config.yaml").write_text(text, encoding="utf-8")
    return HarnessConfig.load(tmp / ".harness", repo_root=tmp)


def test_default_is_300_for_every_phase(tmp_path):
    cfg = _cfg(tmp_path, "model: auto\n")
    assert cfg.timeout_for_phase("context") == 300
    assert cfg.timeout_for_phase("coding") == 300


def test_global_phase_timeout(tmp_path):
    cfg = _cfg(tmp_path, "phase_timeout: 420\n")
    assert cfg.timeout_for_phase("context") == 420
    assert cfg.timeout_for_phase("coding") == 420


def test_per_phase_override_wins(tmp_path):
    cfg = _cfg(tmp_path, "phase_timeout: 300\nphase_timeouts:\n  context: 600\n")
    assert cfg.timeout_for_phase("context") == 600
    assert cfg.timeout_for_phase("coding") == 300


def test_bad_or_zero_override_falls_back(tmp_path):
    cfg = _cfg(tmp_path, "phase_timeout: 360\nphase_timeouts:\n  context: 0\n")
    assert cfg.timeout_for_phase("context") == 360
