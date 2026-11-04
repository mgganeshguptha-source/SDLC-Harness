"""
reference_repos.py — other repos the context phase may read.

THE PROBLEM
A run works on one repo. A story often depends on something that lives
elsewhere: OP018-6434 needed SOAP classes the MPA monolith uses, could not see
them, planned around placeholder names and halted after a full coding phase.

THE LIST
.harness/repo_list.yaml, on the story's branch, so each release branch can name
its own repos and branches:

    default_branch: master
    repos:
      - name: BCBSM/dx_mpa_responsive
        branch: PM_Sep                       # optional override
        purpose: MPA monolith, SOAP clients

The file is mandatory on a fresh run, like the story. `repos: []` is valid and
means no other repo is needed. A resume does not require it: a run that started
before this file existed has none on its working branch.

THE FLOW
1. The context phase sees the list. When it needs one of them, it writes
       REFERENCE REPO NEEDED: <repo> — <why>
2. The harness checks the repo is listed and clones it on its listed branch,
   outside the service repo, with no .git folder. The write boundary covers the
   service repo only, so no phase can change it.
3. Context runs a second pass that fills only that gap and records its findings
   with repo, branch and file.
4. The clones are deleted before the next phase. Later phases use the findings
   in the context file.

One round only: a repo first asked for on the second pass is not cloned.

WHAT THIS IS NOT
Reference repos are not scanned for PII or secrets. The story and attachments
are; these are not. Get the repo owner's approval before listing a repo.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REPO_LIST_FILE = ".harness/repo_list.yaml"
MAX_PER_RUN = 3
CLONE_TIMEOUT_SECONDS = 600
PR_MARKER = "<!-- sdlc-harness-reference-repos -->"

_NAME = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_REQUEST = re.compile(
    r"^[ \t>*-]*\**\s*REFERENCE REPO NEEDED\s*\**\s*:\s*\**\s*`?([A-Za-z0-9_./-]+)`?\**"
    r"[ \t]*(?:[-—–:]+[ \t]*(.*))?$",
    re.IGNORECASE | re.MULTILINE)


@dataclass
class RepoEntry:
    name: str
    branch: str
    source: str          # "default" or "override"
    purpose: str = ""


def load(repo_root) -> Optional[List[RepoEntry]]:
    """The listed repos, or None when the file does not exist.
    Raises ValueError, with every problem named, when the file is invalid."""
    f = Path(repo_root) / REPO_LIST_FILE
    if not f.is_file():
        return None
    import yaml
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{REPO_LIST_FILE} is not valid YAML: {e}")
    if not isinstance(data, dict) or "repos" not in data:
        raise ValueError(f"{REPO_LIST_FILE} must have a 'repos' list (use 'repos: []' "
                         f"when no other repo is needed)")
    repos = data.get("repos")
    if repos is None:
        repos = []
    if not isinstance(repos, list):
        raise ValueError(f"{REPO_LIST_FILE}: 'repos' must be a list")
    default = str(data.get("default_branch") or "").strip()
    problems, entries, seen = [], [], set()
    if default and not _BRANCH.match(default):
        problems.append(f"default_branch '{default}' is not a valid branch name")
    for i, item in enumerate(repos, 1):
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            problems.append(f"repos entry {i} has no name")
            continue
        name = str(item["name"]).strip()
        override = str(item.get("branch") or "").strip()
        branch = override or default
        if not _NAME.match(name):
            problems.append(f"repos entry {i}: '{name}' is not a valid repo name")
        if name.lower() in seen:
            problems.append(f"{name} is listed twice")
        seen.add(name.lower())
        if not branch:
            problems.append(f"{name} has no branch and there is no default_branch")
        elif not _BRANCH.match(branch):
            problems.append(f"{name}: '{branch}' is not a valid branch name")
        entries.append(RepoEntry(name, branch, "override" if override else "default",
                                 str(item.get("purpose") or "").strip()))
    if problems:
        raise ValueError(f"{REPO_LIST_FILE}: " + "; ".join(problems))
    return entries


def fresh_run_problem(repo_root) -> str:
    """'' when a fresh run may start, otherwise what the developer must fix."""
    try:
        entries = load(repo_root)
    except ValueError as e:
        return str(e)
    if entries is not None:
        return ""
    return (f"{REPO_LIST_FILE} was not found on this branch. Every fresh run needs it. "
            f"List the repos this work may need, or write 'repos: []' if none. "
            f"Add '!{REPO_LIST_FILE}' to .gitignore (or use git add -f), commit the "
            f"file to the story branch, and run again.")


def find(entries: List[RepoEntry], requested: str) -> Optional[RepoEntry]:
    """Match the full name, or the name without its owner when that is unique."""
    want = requested.strip().strip("/").lower()
    exact = [e for e in entries if e.name.lower() == want]
    if exact:
        return exact[0]
    short = [e for e in entries if e.name.lower().rsplit("/", 1)[-1] == want]
    return short[0] if len(short) == 1 else None


def parse_requests(context_text: str) -> List[dict]:
    """REFERENCE REPO NEEDED lines, as [{'repo', 'reason'}], one per repo."""
    out, seen = [], set()
    for m in _REQUEST.finditer(context_text or ""):
        repo = m.group(1).strip().strip("/")
        if repo.lower() in ("none", "n/a") or repo.lower() in seen:
            continue
        seen.add(repo.lower())
        out.append({"repo": repo, "reason": (m.group(2) or "").strip()})
    return out


def clones_root(repo_root) -> Path:
    """Beside the service repo, so it is outside every write boundary and can
    never be committed."""
    return Path(repo_root).resolve().parent / "reference-repos"


def clone_path(repo_root, name: str) -> Path:
    return clones_root(repo_root) / name.replace("/", "__")


def remove_clones(repo_root) -> bool:
    root = clones_root(repo_root)
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


def _clone_env(server: str) -> dict:
    """The token travels as a per-process git setting: never in the URL, the
    process arguments or a git config file."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
    if token and server.startswith("http"):
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.{server}/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {auth}"
    return env


def clone(entry: RepoEntry, repo_root) -> str:
    """Shallow-clone `entry` and return its commit. Raises RuntimeError with a
    message the developer can act on; it never contains the token."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    full = entry.name
    if "/" not in full:
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
        if not owner:
            raise RuntimeError(f"{entry.name} has no owner: write it as owner/repo")
        full = f"{owner}/{full}"
    dest = clone_path(repo_root, entry.name)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags",
             "--branch", entry.branch, "--", f"{server}/{full}.git", str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_clone_env(server), timeout=CLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"cloning {full} took over {CLONE_TIMEOUT_SECONDS} seconds")
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"could not clone {full} on branch '{entry.branch}': "
                           f"{r.stderr.strip()[-300:]}")
    sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return sha


def record_line(r: dict) -> str:
    return (f"{r['repo']}  branch={r['branch']}  commit={r['commit']}  ({r['source']})"
            + (f"  reason: {r['reason']}" if r.get("reason") else ""))


def write_job_summary(records: List[dict]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path or not records:
        return
    lines = ["### Reference repos read by context", "",
             "| Repo | Branch | Commit | Branch from | Reason |", "|---|---|---|---|---|"]
    lines += [f"| {r['repo']} | {r['branch']} | {r['commit']} | {r['source']} | "
              f"{r.get('reason', '')} |" for r in records]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def add_to_pr_body(repo_root, records: List[dict]) -> bool:
    """Add the repos read to .harness/pr-body.md. Written by the harness, not the
    model, so the reviewer can rely on it."""
    f = Path(repo_root) / ".harness" / "pr-body.md"
    if not records or not f.is_file():
        return False
    text = f.read_text(encoding="utf-8")
    if PR_MARKER in text:
        return False
    lines = ["", "", PR_MARKER, "## Reference repos read", "",
             "If a branch is wrong, fix `.harness/repo_list.yaml` and run again.", "",
             "| Repo | Branch | Commit | Reason |", "|---|---|---|---|"]
    lines += [f"| {r['repo']} | {r['branch']} | {r['commit']} | {r.get('reason', '')} |"
              for r in records]
    f.write_text(text.rstrip() + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def prompt_block(repo_root, records: List[dict]) -> str:
    """What the context phase is told. Empty when there is nothing to say."""
    if records:
        out = ["", "REFERENCE REPOS (read-only copies of other repos, checked out for "
               "this story):"]
        for r in records:
            out.append(f"  - {r['repo']}, branch {r['branch']}, at "
                       f"{clone_path(repo_root, r['repo'])}"
                       + (f" (needed because: {r['reason']})" if r.get("reason") else ""))
        out += ["Find only what the gap needs: search first, then open the files that "
                "matter. These folders are gone after this phase, so write what later "
                "phases need into the context file, under '## Reference repo findings', "
                "giving the repo, branch and file for each finding. Do not copy secrets, "
                "credentials or member data. Do not write a REFERENCE REPO NEEDED line "
                "for a repo listed here.", ""]
        return "\n".join(out)
    try:
        entries = load(repo_root) or []
    except ValueError:
        return ""
    if not entries:
        return ""
    out = ["", "OTHER REPOS YOU CAN ASK FOR:"]
    out += [f"  - {e.name}" + (f": {e.purpose}" if e.purpose else "") for e in entries]
    out += [f"If this story depends on something that lives in one of these and this "
            f"repo does not show it (a contract, a class, a library, an endpoint), add "
            f"one line per repo to the context file, at most {MAX_PER_RUN}:",
            "  REFERENCE REPO NEEDED: <repo> — <what you need from it>",
            "Still write the context file and keep your open questions. The harness "
            "will check the repo out and run this phase once more. Ask only for what "
            "this story needs.", ""]
    return "\n".join(out)
