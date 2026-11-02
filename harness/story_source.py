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
    run overwriting a shared file. Looks, case-insensitively, for:
      <folder>/<feature_id>/<feature_id>-story.md, <folder>/<feature_id>/<feature_id>.md
        (the story inside its own folder, next to its attachments — e.g.
         stories/PC-102/PC-102.md with stories/PC-102/openapi.yml)
      <folder>/<feature_id>-story.md, <folder>/<feature_id>.md
    where <folder> is the folder of the configured story_file (default
    stories/), and falls back to the configured file itself
    (stories/current-story.md).

    Returns (path, per_story) - per_story is False when the fallback was used,
    so the caller can warn that the story may belong to a different feature.
    """
    configured_path = Path(repo) / configured
    folder = configured_path.parent
    wanted = story_file_names(feature_id)
    if feature_id and folder.is_dir():
        # The story's own folder first (stories/<id>/), then the shared folder.
        sub = next((d for d in folder.iterdir()
                    if d.is_dir() and d.name.lower() == feature_id.lower()), None)
        for where in ([sub] if sub else []) + [folder]:
            by_name = {f.name.lower(): f for f in where.iterdir() if f.is_file()}
            for name in wanted:
                if name in by_name:
                    return by_name[name], True
    return configured_path, False


def story_file_names(feature_id: str) -> list:
    """File names (lower-case) a story for `feature_id` may have."""
    return [f"{feature_id}-story.md".lower(), f"{feature_id}.md".lower()]


def story_hash(text: str) -> str:
    """sha256 of the story text as the harness uses it (stripped)."""
    import hashlib
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def read_story_from_ref(repo: Path, ref: str, feature_id: str,
                        configured: str) -> tuple[str, str]:
    """Read the story for `feature_id` as it is on git ref `ref` (e.g.
    origin/PM_Sep), without touching the working tree.

    A resume runs on the working branch, which holds the story as it was when
    the run started. A developer answers clarifications by editing the story
    on the BASE branch, so that is where a resume must read it from. Same
    precedence as resolve_story_file. Returns (text, repo-relative path).
    Raises FileNotFoundError / ValueError like FileStorySource.
    """
    import subprocess
    folder = str(Path(configured).parent).replace("\\", "/")
    folder = "" if folder in (".", "") else folder.rstrip("/") + "/"

    def _git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    listing = _git("ls-tree", "--name-only", ref, "--", folder or ".")
    if listing.returncode != 0:
        raise FileNotFoundError(f"cannot list {folder or '.'} on {ref}: "
                                f"{listing.stderr.strip()}")
    entries = [n for n in listing.stdout.splitlines() if n]
    path = None
    # Same precedence as resolve_story_file: the story's own folder first.
    sub = next((n for n in entries
                if feature_id and Path(n).name.lower() == feature_id.lower()), None)
    places = []
    if sub:
        sl = _git("ls-tree", "--name-only", ref, "--", sub.rstrip("/") + "/")
        if sl.returncode == 0:
            places.append([n for n in sl.stdout.splitlines() if n])
    places.append(entries)
    for names in places:
        by_name = {Path(n).name.lower(): n for n in names}
        for name in story_file_names(feature_id):
            if feature_id and name in by_name:
                path = by_name[name]
                break
        if path:
            break
    path = path or configured.replace("\\", "/")
    shown = _git("show", f"{ref}:{path}")
    if shown.returncode != 0:
        raise FileNotFoundError(f"story file {path} not found on {ref}")
    text = shown.stdout.strip()
    if not text:
        raise ValueError(f"Story file is empty: {path} on {ref}")
    return text, path


# Future:
# class JiraMcpStorySource:
#     def __init__(self, issue_key, mcp_client): ...
#     def get_story(self) -> str:
#         # call Jira via MCP, return the formatted story
#         ...
