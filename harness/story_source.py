"""
story_source.py — where the harness gets the user story.

A clean seam so the *source* of the story is swappable without touching the rest
of the harness:
  - FileStorySource  : reads a markdown file (demo; an MCP step would populate it)
  - JiraMcpStorySource: (future) fetch from Jira via MCP — same interface

In production, a pre-step (MCP -> Jira) writes the story file; the harness just
reads it. The harness does not care HOW the story arrived. Today we read a
committed file so CI can see it.
"""
from __future__ import annotations
from pathlib import Path
from typing import Protocol


class StorySource(Protocol):
    def get_story(self) -> str: ...


class FileStorySource:
    """Reads the story from a markdown file (repo-relative or absolute)."""
    def __init__(self, path: Path):
        self.path = Path(path)

    def get_story(self) -> str:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Story file not found: {self.path}. "
                f"In production an MCP/Jira step writes this; for the demo, create it."
            )
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Story file is empty: {self.path}")
        return text


def resolve_story_file(repo: Path, feature_id: str, configured: str) -> tuple[Path, bool]:
    """Pick the story file for a run.

    One file per story lets several stories sit side by side instead of each
    run overwriting a shared file. Looks in the folder of the configured
    story_file (default stories/) for, in order:
      <feature_id>-story.md, <feature_id>.md   (case-insensitive)
    and falls back to the configured file itself (stories/current-story.md).

    Returns (path, per_story) - per_story is False when the fallback was used,
    so the caller can warn that the story may belong to a different feature.
    """
    configured_path = Path(repo) / configured
    folder = configured_path.parent
    wanted = [f"{feature_id}-story.md".lower(), f"{feature_id}.md".lower()]
    if feature_id and folder.is_dir():
        by_name = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
        for name in wanted:
            if name in by_name:
                return by_name[name], True
    return configured_path, False


# Future:
# class JiraMcpStorySource:
#     def __init__(self, issue_key, mcp_client): ...
#     def get_story(self) -> str:
#         # call Jira via MCP, return the formatted story
#         ...
