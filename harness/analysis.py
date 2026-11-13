"""
analysis.py — the harness side of ANALYSIS MODE (bug-fix analysis).

In analysis mode the model writes ONE file, .harness/bug-analysis.md, following
the toolkit's analyze-bug template. Everything else is done here, in code:

  scan()       the ANALYSIS GATE. An open [NEEDS CLARIFICATION] line means the
               evidence was not enough to name a root cause; the run halts for
               the reporter instead of publishing a guess.
  publish()    stamps the document with the source it was analysed against
               (commit, branch, story hash) and copies it to the story folder,
               <attachments_dir>/<feature_id>-bug-analysis.md.
  write_pr_body()  the pull-request text, with the reviewer's checklist.

WHY THE STORY FOLDER
A lead reviews the document in a document-only pull request and merges it into
the story branch. A later BUILD run of the same story then finds it next to the
story file, where attachments.py already sends every .md to the phases that
plan and write the fix. The two runs share nothing else — no state, no working
branch — so the repo may change between them.

WHY THE HARNESS STAMPS THE SOURCE
The build run needs to know which commit the analysis read, to tell whether the
code it cites has changed since. A model asked to write a commit id invents one.

NOT HERE YET (later steps of the approach): engine-side evidence collection
(callers of the touched symbols), the evidence gates beyond open questions, and
the build run's drift check against the analysed commit.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from clarification import _is_real_marker_line
from phases import ANALYSIS_DOC

# The engine-written block at the top of the published document. Replaced, not
# appended, when a document that already has one is published again.
STAMP_BEGIN = "<!-- harness:analysed-source:begin -->"
STAMP_END = "<!-- harness:analysed-source:end -->"
_STAMP_RE = re.compile(re.escape(STAMP_BEGIN) + r".*?" + re.escape(STAMP_END) + r"\n*",
                       re.DOTALL)

DOC_SUFFIX = "-bug-analysis.md"


def workspace_doc(repo_root: Path) -> Path:
    """Where the model writes the document during the run."""
    return Path(repo_root) / ANALYSIS_DOC


def doc_rel(cfg, feature_id: str) -> str:
    """Repo-relative path of the published document: the story's own folder
    (config attachments_dir, default stories/<feature_id>/)."""
    folder = (getattr(cfg, "attachments_dir", "") or "stories/{feature_id}")
    folder = folder.replace("{feature_id}", feature_id).strip().strip("/")
    return f"{folder}/{feature_id}{DOC_SUFFIX}"


def scan(doc: Path) -> list:
    """Open questions in the analysis document: every genuine
    [NEEDS CLARIFICATION] line (same marker and same filtering as the context
    gate, so 'None.' / 'No [NEEDS CLARIFICATION] items' lines do not count)."""
    try:
        text = Path(doc).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items = []
    for line in text.splitlines():
        if _is_real_marker_line(line):
            items.append(line.strip().lstrip("-*> \t").strip())
    return items


def _git(repo_root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def analysed_source(repo_root: Path, run) -> dict:
    """What the analysis read, as facts the harness can vouch for."""
    return {
        "commit": _git(repo_root, "rev-parse", "HEAD") or "unknown",
        "branch": ((os.environ.get("HARNESS_BASE_BRANCH") or "").strip()
                   or _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"),
        "analysed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "story_path": getattr(run, "story_path", None) or "unknown",
        "story_sha256": (getattr(run, "story_sha256", None) or "")[:12] or "unknown",
        "run_id": os.environ.get("GITHUB_RUN_ID") or "local",
        "actor": os.environ.get("GITHUB_ACTOR") or getattr(run, "actor", None) or "local",
    }


def stamp_block(src: dict) -> str:
    return (
        f"{STAMP_BEGIN}\n"
        "> **Analysed source (recorded by the harness, not by the model)**\n"
        f"> - Commit: `{src['commit']}`\n"
        f"> - Branch: `{src['branch']}`\n"
        f"> - Analysed at (UTC): {src['analysed_at']}\n"
        f"> - Bug report: `{src['story_path']}` @ `{src['story_sha256']}`\n"
        f"> - Harness run: {src['run_id']} by {src['actor']}\n"
        ">\n"
        "> File and line references below are valid for this commit. The code may\n"
        "> have changed since; check before relying on them.\n"
        f"{STAMP_END}\n\n"
    )


def stamped(text: str, src: dict) -> str:
    """`text` with the analysed-source block placed directly under its first
    heading (or at the top when it has none). An existing block is replaced."""
    body = _STAMP_RE.sub("", text).lstrip("\n")
    block = stamp_block(src)
    lines = body.split("\n")
    if lines and lines[0].startswith("#"):
        rest = "\n".join(lines[1:]).lstrip("\n")
        return f"{lines[0]}\n\n{block}{rest}".rstrip("\n") + "\n"
    return (block + body).rstrip("\n") + "\n"


def publish(repo_root: Path, cfg, run) -> str:
    """Stamp the workspace document and copy it to the story folder. Returns the
    repo-relative path written. Raises FileNotFoundError when the model wrote
    no document (the executor's required-artifact check normally catches that
    first)."""
    repo_root = Path(repo_root)
    src_doc = workspace_doc(repo_root)
    text = src_doc.read_text(encoding="utf-8", errors="replace")
    rel = doc_rel(cfg, run.feature_id)
    out = repo_root / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    final = stamped(text, analysed_source(repo_root, run))
    out.write_text(final, encoding="utf-8")
    # Keep the workspace copy identical, so the audit folder holds what was
    # published rather than the unstamped draft.
    src_doc.write_text(final, encoding="utf-8")
    return rel


def write_pr_body(repo_root: Path, run, rel: str) -> Path:
    """The document-only pull request's text. Written by the harness: the PR
    holds one file, so a model summary of it would add cost and nothing else."""
    fid = run.feature_id
    body = (
        f"## Bug analysis for {fid} — for review\n\n"
        f"This pull request adds one file: `{rel}`. It holds the harness's "
        "root-cause and impact analysis of the bug. **No code was changed.**\n\n"
        "### What to review\n\n"
        "- [ ] Root cause: do the cited files and lines really produce the symptom?\n"
        "- [ ] Alternatives: are the causes it ruled out really ruled out?\n"
        "- [ ] Fix option: is the recommended option the one you want built?\n"
        "- [ ] Impact: is anything missing — a caller, a consumer in another "
        "repo, shared data or configuration?\n"
        "- [ ] Regression scope: is it enough to release the fix?\n\n"
        "Correct the document in this pull request where it is wrong, and fill "
        "its **Lead approval** section. Merging this pull request is the approval.\n\n"
        "### What happens next\n\n"
        f"After the merge, run the harness for `{fid}` again with `mode: build` "
        "and the same base branch. That run reads this document from the story "
        "folder, together with the code as it is then, and makes the fix.\n\n"
        "### Limits of this analysis\n\n"
        "- It was made by reading the code. Nothing was compiled or run.\n"
        "- Callers and consumers were found by the model reading and searching "
        "this repository. Other repositories were not read.\n"
        "- File and line references are valid for the commit recorded at the top "
        "of the document.\n"
    )
    p = Path(repo_root) / ".harness" / "pr-body.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p
