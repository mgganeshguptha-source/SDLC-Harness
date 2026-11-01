"""A MANUAL_CHANGE_REQUIRED declaration naming a file the phase may write is
sent back once instead of halting (petclinic-rest-api PC-101, 2026-11-01)."""
from pathlib import Path

from boundaries import is_write_allowed
from config import HarnessConfig
from phases import PHASES


def _coding():
    return next(p for p in PHASES if p.id == "coding")


def test_existing_main_file_is_writable_for_coding(tmp_path: Path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.yaml").write_text("repo_stacks: ['backend']\n")
    cfg = HarnessConfig.load(tmp_path / ".harness", repo_root=tmp_path)
    ph = _coding()
    f = "src/main/java/org/x/repository/jdbc/JdbcOwnerRepositoryImpl.java"
    assert is_write_allowed(f, cfg.phase_writes(ph), repo_root=str(tmp_path),
                            exclude_globs=cfg.phase_exclude(ph))
    # A genuine manual change stays a manual change.
    assert not is_write_allowed("pom.xml", cfg.phase_writes(ph), repo_root=str(tmp_path),
                                exclude_globs=cfg.phase_exclude(ph))
