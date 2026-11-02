"""Story inside its own folder, next to its attachments:
stories/PC-102/PC-102.md + stories/PC-102/openapi.yml (copied as is)."""
import subprocess
from pathlib import Path

import attachments as A
from story_source import read_story_from_ref, resolve_story_file

CONFIGURED = "stories/current-story.md"


def _layout(tmp_path: Path) -> Path:
    d = tmp_path / "stories" / "PC-102"
    d.mkdir(parents=True)
    (d / "PC-102.md").write_text("Title: in folder\n")
    (d / "openapi.yml").write_text("openapi: 3.0.1\ninfo:\n  title: Spring PetClinic\n")
    return tmp_path


def test_story_found_in_its_own_folder(tmp_path):
    repo = _layout(tmp_path)
    path, per_story = resolve_story_file(repo, "PC-102", CONFIGURED)
    assert per_story and path == repo / "stories/PC-102/PC-102.md"


def test_flat_story_still_found(tmp_path):
    (tmp_path / "stories").mkdir()
    (tmp_path / "stories" / "PC-7.md").write_text("Title: flat\n")
    path, per_story = resolve_story_file(tmp_path, "PC-7", CONFIGURED)
    assert per_story and path.name == "PC-7.md"


def test_folder_story_wins_over_flat(tmp_path):
    repo = _layout(tmp_path)
    (repo / "stories" / "PC-102.md").write_text("Title: flat\n")
    path, _ = resolve_story_file(repo, "PC-102", CONFIGURED)
    assert path.parent.name == "PC-102"


def test_story_is_not_an_attachment(tmp_path):
    repo = _layout(tmp_path)
    names = [p.name for p in A.discover(repo / "stories" / "PC-102")]
    assert names == ["openapi.yml"]


def test_story_read_from_ref_in_folder(tmp_path):
    repo = _layout(tmp_path)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                  capture_output=True)
    g("init", "-q")
    g("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s")
    text, path = read_story_from_ref(repo, "HEAD", "PC-102", CONFIGURED)
    assert text == "Title: in folder" and path == "stories/PC-102/PC-102.md"
