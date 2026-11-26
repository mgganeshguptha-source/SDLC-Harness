"""
pr_evidence.py — the reviewer-facing PR body, split by who may be trusted for what.

WHY THIS EXISTS
The raise_pr phase used to ask the model to "summarize the change". That gave
the reviewer a prose summary in which the model also described the tests and the
build — claims nobody had checked, written by the same agent that wrote the code.

The body is now written in two parts:

  1. THE MODEL (pr-description skill) writes what only a reader of the change
     can write: why it exists (one sentence), the shape of the change (small
     visuals), where to look hardest, and a merge-risk assessment that is
     labelled as an AI assessment for the reviewer to confirm.

  2. THE ENGINE (this module) appends the Evidence section from the gate
     outputs it already holds: the build gate, the per-criterion verdicts the
     AC gate parsed, the code-review verdict and its non-blocking notes, how
     many times each gate sent work back, and the test files the run changed.
     Nothing in it is model-written, so the reviewer can rely on it.

prompt_block() gives the model the changed-file list and a bounded diff, so it
draws its visuals from the change itself instead of exploring the repository
(raise_pr may not run shell, so it cannot run git diff itself).

Every function here is best-effort: a failure returns '' / False and never
fails the run. The PR is raised either way; it just carries less detail.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PR_MARKER = "<!-- harness:pr-evidence -->"

# Bounded so a large change cannot blow the raise_pr prompt. Over the cap the
# model gets the file list and stat only, and reads the files it needs.
MAX_DIFF_CHARS = 40000
MAX_LISTED_FILES = 60
MAX_NOTES = 10

_EXCLUDED_PREFIXES = (".harness/", "audit/", "metrics/", "contracts/")


def _git(repo_root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo_root),
                           capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _is_test(path: str) -> bool:
    p = f"/{path}"
    return "/test/" in p or path.endswith((".spec.ts", ".test.ts"))


def _changed(repo_root: Path) -> list:
    """Files this run changed or added, from git, harness-only paths excluded."""
    files: list = []
    try:
        import handoff as _HO
        files = _HO.changed_files(repo_root, _HO.base_ref_from_env())
    except Exception:
        files = []
    if not files:
        out = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
        files = sorted({ln[3:].strip().split(" -> ")[-1] for ln in out.splitlines()
                        if ln[3:].strip()})
    return [f for f in files if not f.startswith(_EXCLUDED_PREFIXES)]


def _diff(repo_root: Path, files: list) -> tuple:
    """(stat, diff) of tracked changes against the merge-base with the base
    branch, or against HEAD when no base is known. ('', '') on failure."""
    base = ""
    try:
        import handoff as _HO
        ref = _HO.base_ref_from_env()
        if ref:
            base = _git(repo_root, "merge-base", ref, "HEAD").strip()
    except Exception:
        base = ""
    rev = base or "HEAD"
    tracked = [f for f in files if _git(repo_root, "ls-files", "--", f).strip()]
    if not tracked:
        return "", ""
    stat = _git(repo_root, "diff", "--stat", rev, "--", *tracked)
    diff = _git(repo_root, "diff", rev, "--", *tracked)
    return stat, diff


def prompt_block(repo_root, run) -> str:
    """What the raise_pr model is told about the change. '' when unknown."""
    try:
        root = Path(repo_root)
        files = _changed(root)
        if not files:
            return ""
        tests = [f for f in files if _is_test(f)]
        other = [f for f in files if f not in tests]
        lines = ["", "CHANGED FILES (listed by the harness from git):"]
        lines += [f"  production / other: {f}" for f in other[:MAX_LISTED_FILES]]
        lines += [f"  test: {f}" for f in tests[:MAX_LISTED_FILES]]
        if len(other) > MAX_LISTED_FILES or len(tests) > MAX_LISTED_FILES:
            lines.append(f"  ... {len(files)} files in total")
        new = [f for f in files if not _git(root, "ls-files", "--", f).strip()]
        if new:
            lines.append("New files (not in the diff below; open them to see their "
                         "shape): " + ", ".join(new[:MAX_LISTED_FILES]))
        stat, diff = _diff(root, files)
        if diff and len(diff) <= MAX_DIFF_CHARS:
            lines += ["", "--- GIT DIFF (this run's changes to existing files) ---",
                      diff.rstrip(), "--- END DIFF ---"]
        elif stat:
            lines += ["", f"The diff is too large to include ({len(diff)} characters). "
                      "Diff stat:", stat.rstrip(),
                      "Open only the files you need for the visuals."]
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


def _md(text: str) -> str:
    """One line, safe inside a markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def evidence_lines(repo_root, run) -> list:
    """The Evidence section, built from the gate outputs only."""
    root = Path(repo_root)
    hd = root / ".harness"
    out = ["", "", PR_MARKER, "## Evidence (written by the harness, not the AI)", ""]

    # ---- build and tests ----
    try:
        from config import harness_runs_build, HarnessConfig
        runs_build = harness_runs_build(hd)
    except Exception:
        runs_build = None
    if runs_build:
        cmd = ""
        try:
            cmd = HarnessConfig.load(hd, repo_root=root).resolved_test_command()
        except Exception:
            cmd = ""
        out.append("**Build and tests:** the harness built this exact code and ran "
                   + (f"`{_md(cmd)}`" if cmd else "the repo's test command")
                   + " at its build gate; it passed (the run stops otherwise). "
                   "Output: `validation-report.txt` in this run's audit folder on "
                   "`harness-metrics`.")
    elif runs_build is False:
        out += ["**Build and tests:** NOT run by the harness (`runs_build: false`).",
                "",
                "**How to verify:** build the project and run its tests locally "
                "before approving."]
    out.append("")

    # ---- acceptance criteria ----
    try:
        import ac_validation as _AC
        if (hd / "validation.md").is_file():
            v = _AC.scan_validation(root, hd)
            rows = ([(a, "MET") for a in v.met] + [(a, "NOT_MET") for a in v.not_met]
                    + [(a, "UNVERIFIABLE") for a in v.unverifiable]
                    + [(a, "NO VERDICT") for a in v.unvalidated])
            if rows:
                def _key(r):
                    try:
                        return [int(x) for x in r[0].split("-", 1)[1].split(".")]
                    except Exception:
                        return [10 ** 6]
                out += [f"**Acceptance criteria:** gate verdict `{v.verdict}`. "
                        "Per-criterion evidence: `validation.md` in the audit folder.",
                        "", "| Criterion | Verdict |", "|---|---|"]
                out += [f"| {a} | {s} |" for a, s in sorted(rows, key=_key)]
                out.append("")
    except Exception:
        pass

    # ---- code review ----
    try:
        import review as _RV
        rf = hd / "review.md"
        if rf.is_file():
            r = _RV.parse_review(rf)
            loops = int(getattr(run, "review_attempts", 0) or 0)
            line = f"**Code review (AI reviewer):** `{r.verdict or 'NO VERDICT'}`"
            if loops:
                line += (f" after {loops} loop-back{'s' if loops != 1 else ''} "
                         "(since the run started or was last resumed)")
            out += [line + ". Full review: `review.md` in the audit folder.", ""]
            # parse_review keeps [NOTE] lines only when it downgraded a verdict;
            # on a plain PASS read them from the file so the reviewer sees them.
            notes = list(r.advisory or [])
            if not notes:
                try:
                    notes = [n.strip() for n in _RV._NOTE.findall(
                        rf.read_text(encoding="utf-8", errors="replace")) if n.strip()]
                except Exception:
                    notes = []
            if notes:
                out.append("Non-blocking notes from the reviewer, for your judgement:")
                out += [f"- {_md(n)}" for n in notes[:MAX_NOTES]]
                if len(notes) > MAX_NOTES:
                    out.append(f"- ... and {len(notes) - MAX_NOTES} more in review.md")
                out.append("")
    except Exception:
        pass

    # ---- gate loop-backs ----
    loops = [(n, int(getattr(run, a, 0) or 0)) for n, a in (
        ("build", "validation_attempts"), ("compile check", "compile_attempts"),
        ("acceptance criteria", "ac_attempts"), ("coverage", "coverage_attempts"),
        ("plan scope", "scope_attempts"))]
    loops = [(n, c) for n, c in loops if c]
    if loops:
        out += ["**Gates that sent work back before passing** (since the run "
                "started or was last resumed): "
                + ", ".join(f"{n} ({c})" for n, c in loops) + ".", ""]

    # ---- test files ----
    try:
        tests = [f for f in _changed(root) if _is_test(f)]
        if tests:
            out.append(f"**Test files changed or added ({len(tests)}):**")
            out += [f"- `{t}`" for t in tests[:MAX_LISTED_FILES]]
            if len(tests) > MAX_LISTED_FILES:
                out.append(f"- ... and {len(tests) - MAX_LISTED_FILES} more")
            out.append("")
    except Exception:
        pass

    out.append("_The harness never merges. A person approves and merges this PR._")
    return out


def add_to_pr_body(repo_root, run) -> bool:
    """Append the Evidence section to .harness/pr-body.md. Build mode only:
    unit-test mode appends its own measured coverage table (unit_test_mode.py)."""
    try:
        try:
            import unit_test_mode as _UT
            if _UT.is_unit_test(run):
                return False
        except Exception:
            pass
        f = Path(repo_root) / ".harness" / "pr-body.md"
        if not f.is_file():
            return False
        text = f.read_text(encoding="utf-8")
        if PR_MARKER in text:
            return False
        f.write_text(text.rstrip() + "\n".join(evidence_lines(repo_root, run)) + "\n",
                     encoding="utf-8")
        return True
    except Exception:
        return False
