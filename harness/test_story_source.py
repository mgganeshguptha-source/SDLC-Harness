"""
test_story_source.py - which story file a run reads.

Run:  python -m pytest harness/test_story_source.py -v
(or:  python harness/test_story_source.py   for a no-pytest fallback)
"""
import tempfile
from pathlib import Path

from story_source import resolve_story_file

_CONFIGURED = "stories/current-story.md"


def _repo(*names):
    root = Path(tempfile.mkdtemp())
    (root / "stories").mkdir()
    for n in names:
        (root / "stories" / n).write_text("story", encoding="utf-8")
    return root


def test_per_story_file_wins_over_shared_file():
    repo = _repo("current-story.md", "OPO18-456-story.md")
    path, per_story = resolve_story_file(repo, "OPO18-456", _CONFIGURED)
    assert path.name == "OPO18-456-story.md" and per_story


def test_plain_feature_name_also_accepted():
    repo = _repo("OPO18-456.md")
    path, per_story = resolve_story_file(repo, "OPO18-456", _CONFIGURED)
    assert path.name == "OPO18-456.md" and per_story


def test_match_ignores_case():
    repo = _repo("opo18-456-STORY.md")
    path, per_story = resolve_story_file(repo, "OPO18-456", _CONFIGURED)
    assert path.name == "opo18-456-STORY.md" and per_story


def test_other_stories_are_not_picked():
    repo = _repo("current-story.md", "OPO18-999-story.md")
    path, per_story = resolve_story_file(repo, "OPO18-456", _CONFIGURED)
    assert path.name == "current-story.md" and not per_story


def test_falls_back_to_configured_file():
    repo = _repo("current-story.md")
    path, per_story = resolve_story_file(repo, "OPO18-456", _CONFIGURED)
    assert path == repo / _CONFIGURED and not per_story


# ---- no-pytest fallback runner ------------------------------------------
if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}  {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
