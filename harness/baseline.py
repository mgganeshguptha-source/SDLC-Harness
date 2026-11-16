"""
baseline.py — the baseline build check.

WHY
The build gate after unit_testing runs the repo's WHOLE test command. Until now
every red result was treated as caused by the story. On OP018-6434 (2026-11-15)
tests that have nothing to do with the story were red, the failure was sent to
'coding', and coding changed five unplanned production files — removing an
access check on the way — to make them pass. A missing library was also found
only after the model phases had run.

WHAT IT DOES (only when runs_build is true, build mode)
Before the first AI phase of a fresh run, the harness runs the repo's own
test_command once on the untouched base code:

  - missing library, toolchain, network   -> environment halt, before any model
                                             call (not saved: it is not a
                                             property of the code)
  - compile error, or a build that fails
    without per-test results              -> halt "existing build failure"; the
                                             developer fixes the base branch and
                                             starts a fresh run
  - tests that already fail               -> recorded as "already failing" and
                                             the run CONTINUES. The build gate
                                             then fails only on tests that are
                                             not on that list.
  - everything passes                     -> the run continues as before

The result is saved on the harness-metrics branch, keyed by the base commit,
the library-store commit and the test command:

    metrics/baseline/<base>-<store>-<command>.json

so the extra build happens once per change of the base code, not on every run.
A resume never repeats it: the result travels in run-state.json.

HOW ALREADY-FAILING TESTS ARE IGNORED (Maven)
With failing tests Maven stops at the first module that has them, so later
modules are never compiled or tested. When there is a list of already-failing
tests the build runs with -Dmaven.test.failure.ignore=true (passed through
MAVEN_OPTS and MAVEN_ARGS, so the repo's test_command is not edited): Maven
then runs every module and exits 0 unless something does not compile, and the
harness reads the per-test results from the surefire reports. A repo with a
clean baseline is built exactly as before.

Builds that do not write surefire/failsafe XML reports (Angular/Karma, Gradle)
cannot be itemised: a baseline failure there halts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

METRICS_BRANCH = "harness-metrics"
RECORD_DIR = "metrics/baseline"
RECORD_SCHEMA = 1
IGNORE_FLAG = "-Dmaven.test.failure.ignore=true"
PR_MARKER = "<!-- harness:baseline-failing-tests -->"
_REPORT_GLOBS = ("target/surefire-reports/TEST-*.xml",
                 "target/failsafe-reports/TEST-*.xml")
_SKIP_PARTS = {"node_modules", ".harness", ".git"}
# How many test ids a prompt or a halt message lists before "... and N more".
_LIST_CAP = 40


# ------------------------------------------------------------------ build env
def tolerant_env(env: dict | None = None) -> dict:
    """The environment for a build whose test failures must not stop Maven.

    MAVEN_OPTS sets the property in Maven's JVM (every Maven 3 version reads it);
    MAVEN_ARGS adds it to the command line on Maven 3.9+. Both are harmless for a
    command that is not Maven."""
    e = dict(os.environ if env is None else env)
    for var in ("MAVEN_OPTS", "MAVEN_ARGS"):
        cur = e.get(var, "") or ""
        if IGNORE_FLAG not in cur:
            e[var] = (cur + " " + IGNORE_FLAG).strip()
    return e


# --------------------------------------------------------------- test results
def read_test_results(repo_root, since: float | None = None) -> tuple[list, int]:
    """(failing test ids, number of tests seen) from surefire/failsafe XML.

    A test id is '<class>#<method>'. `since` (a time.time() value) leaves out
    reports an earlier build wrote: a test_command without `clean` keeps old
    report files, and a deleted test must not count as still failing."""
    import xml.etree.ElementTree as ET
    failing, seen = set(), set()
    root = Path(repo_root)
    for pattern in _REPORT_GLOBS:
        try:
            files = list(root.rglob(pattern))
        except Exception:
            files = []
        for f in files:
            if _SKIP_PARTS & set(f.relative_to(root).parts):
                continue
            try:
                if since is not None and f.stat().st_mtime < since - 2:
                    continue
                suite = ET.parse(str(f)).getroot()
            except Exception:
                continue
            for tc in suite.iter("testcase"):
                cls = tc.get("classname") or suite.get("name") or f.stem[5:]
                tid = f"{cls}#{tc.get('name') or ''}"
                seen.add(tid)
                if tc.find("failure") is not None or tc.find("error") is not None:
                    failing.add(tid)
    return sorted(failing), len(seen)


def split_new(failing_now: list, known: list) -> tuple[list, list]:
    """(new failures, already-failing ones still failing)."""
    k = set(known or [])
    return ([t for t in failing_now if t not in k],
            [t for t in failing_now if t in k])


def class_of(test_id: str) -> str:
    return str(test_id).split("#", 1)[0]


def failure_lines(new: list, ignored: int, tests_seen: int) -> str:
    """The new failures in the form Maven prints them, so the state machine's
    routing (test side or production side) reads them like any red build."""
    out = ["--- NEW FAILING TESTS (listed by the harness from the surefire reports) ---",
           f"Tests run: {tests_seen}, new failures: {len(new)}, "
           f"already failing on the base branch and ignored: {ignored}"]
    for cls in sorted({class_of(t) for t in new}):
        out.append(f"[ERROR] <<< FAILURE! -- in {cls}")
    for t in new:
        cls, _, method = t.partition("#")
        if re.fullmatch(r"[\w$]+", method or ""):
            out.append(f"[ERROR] {cls}.{method} -- Time elapsed: see report <<< FAILURE!")
        else:
            out.append(f"[ERROR] {cls} :: {method}")
    if ignored:
        out.append(f"NOTE: {ignored} other test(s) already fail on the base branch without "
                   f"this change. They are ignored: do not change them, and do not change "
                   f"the code they cover to make them pass.")
    return "\n".join(out)


# ------------------------------------------------------------------- identity
def _git(repo_root, *args, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True,
                          text=True, timeout=timeout)


def _head(path) -> str:
    try:
        r = _git(path, "rev-parse", "HEAD")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def store_dir(repo_root) -> Path | None:
    """The library-store checkout (workflow step 4a puts it next to service/)."""
    for base in (os.environ.get("GITHUB_WORKSPACE"), str(Path(repo_root).parent)):
        if base:
            d = Path(base) / "maven-libs"
            if (d / ".git").exists():
                return d
    return None


def ids(repo_root) -> dict:
    sd = store_dir(repo_root)
    return {"base_commit": _head(repo_root),
            "store_commit": _head(sd) if sd else ""}


def record_key(base_commit: str, store_commit: str, command: str) -> str:
    ch = hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:8]
    return f"{(base_commit or 'nobase')[:12]}-{(store_commit or 'nostore')[:12]}-{ch}"


# ---------------------------------------------------------------- saved result
def load_saved(repo_root, key: str, log=print) -> dict | None:
    """A saved baseline result for this key from harness-metrics, or None."""
    rel = f"{RECORD_DIR}/{key}.json"
    try:
        # A copy written earlier in this checkout (not yet published).
        local = Path(repo_root) / rel
        if local.is_file():
            return json.loads(local.read_text(encoding="utf-8"))
        ref = "FETCH_HEAD"
        f = _git(repo_root, "fetch", "-q", "origin", METRICS_BRANCH, timeout=120)
        if f.returncode != 0:
            ref = f"origin/{METRICS_BRANCH}"   # fetched at checkout, or absent
        r = _git(repo_root, "show", f"{ref}:{rel}")
        if r.returncode != 0:
            return None
        rec = json.loads(r.stdout)
        return rec if isinstance(rec, dict) and rec.get("status") else None
    except Exception as e:
        log(f"  [baseline] saved result not read ({type(e).__name__}); building")
        return None


def save(repo_root, key: str, rec: dict, log=print) -> None:
    """Write the result under metrics/baseline/. The workflow's publish step
    copies metrics/ to harness-metrics; nothing here pushes."""
    try:
        d = Path(repo_root) / RECORD_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps(rec, indent=2, sort_keys=True),
                                       encoding="utf-8")
        log(f"  [baseline] result saved for later runs: {RECORD_DIR}/{key}.json "
            f"(published to {METRICS_BRANCH})")
    except Exception as e:
        log(f"  [baseline] result not saved ({type(e).__name__}: {e})")


# ------------------------------------------------------------------ the check
def needed(run, harness_dir) -> tuple[bool, str]:
    """(run the check?, why not)."""
    from config import harness_runs_build
    if (getattr(run, "mode", None) or "build") != "build":
        return False, "analysis mode does not build"
    if not harness_runs_build(harness_dir):
        return False, "runs_build is false"
    have = (getattr(run, "baseline", None) or {}).get("status")
    if have in ("pass", "tolerated"):
        return False, ""
    if getattr(run, "completed_phases", None):
        # The tree already holds this run's changes, so it is no longer the
        # base code. Only runs started before this check existed get here.
        return False, "this run started without one and has already changed code"
    return True, ""


def _classify(vr) -> str:
    if vr.passed:
        return "tolerated" if vr.failing_tests else "pass"
    if getattr(vr, "failure_kind", None) == "environment" or vr.exit_code < 0:
        return "environment"       # includes TIMEOUT (-1) and launch errors (-2)
    return "failed"


def build(repo_root, harness_dir, log=print) -> tuple[dict, object]:
    """Run test_command on the tree as it stands. Returns (record, result)."""
    from validation import run_validation
    vr = run_validation(Path(repo_root), Path(harness_dir), log=log, baseline=True)
    try:
        from protected_paths import restore_worktree
        restore_worktree(repo_root, harness_dir)
    except Exception:
        pass
    rec = {
        "record_type": "baseline",
        "baseline_schema": RECORD_SCHEMA,
        "status": _classify(vr),
        "summary": str(vr.summary or "").replace("http://", "hxxp://"),
        "exit_code": vr.exit_code,
        "failing_tests": list(vr.failing_tests or []),
        "tests_seen": int(getattr(vr, "tests_seen", 0) or 0),
        "detail": (vr.output_tail or "")[-6000:].replace("http://", "hxxp://"),
        "env_reason": getattr(vr, "env_reason", None),
        "env_hint": getattr(vr, "env_hint", "") or "",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return rec, vr


def ensure(run, repo_root, harness_dir, log=print) -> bool:
    """Run (or reuse) the baseline check. True = carry on; False = the run was
    halted here and run-state.json says why."""
    want, why = needed(run, harness_dir)
    if not want:
        if why:
            log(f"  [baseline] check skipped — {why}")
        elif known_failing(run):
            log(f"  [baseline] {len(known_failing(run))} test(s) already failing on the "
                f"base branch stay ignored (checked when this run started)")
        return True

    from config import HarnessConfig
    cfg = HarnessConfig.load(Path(harness_dir))
    cmd = cfg.resolved_test_command()
    i = ids(repo_root)
    key = record_key(i["base_commit"], i["store_commit"], cmd)
    # A resume builds what is on the working branch, which is not the base
    # commit: never read or write the saved result for it.
    resumed = os.environ.get("HARNESS_RESUMED", "").strip().lower() == "true"
    use_saved = bool(i["base_commit"]) and not resumed

    log("\n=== Baseline build check (before any AI phase) ===")
    log(f"  [baseline] base commit {i['base_commit'][:12] or '(unknown)'}, library store "
        f"{i['store_commit'][:12] or '(none)'}, command: {cmd}")
    rec = load_saved(repo_root, key, log) if use_saved else None
    reused = rec is not None
    if reused:
        log(f"  [baseline] already checked on {rec.get('checked_at', '?')} for this base "
            f"commit, library store and command — not building again")
    else:
        rec, _vr = build(repo_root, harness_dir, log)
        rec.update({"base_commit": i["base_commit"], "store_commit": i["store_commit"],
                    "base_branch": os.environ.get("HARNESS_BASE_BRANCH") or "",
                    "repo": os.environ.get("GITHUB_REPOSITORY") or "",
                    "test_command": cmd,
                    "first_run_id": os.environ.get("GITHUB_RUN_ID") or "local"})
        if use_saved and rec["status"] in ("pass", "tolerated", "failed"):
            save(repo_root, key, rec, log)

    run.baseline = {
        "status": rec.get("status"),
        "failing_tests": list(rec.get("failing_tests") or []),
        "tests_seen": rec.get("tests_seen"),
        "base_commit": rec.get("base_commit") or i["base_commit"],
        "store_commit": rec.get("store_commit") or i["store_commit"],
        "checked_at": rec.get("checked_at"),
        "reused": reused,
    }
    status = rec.get("status")
    if status == "pass":
        log("  [baseline] the base code builds and its tests pass — any later red "
            "build comes from this run's changes")
        run.save(Path(harness_dir))
        return True
    if status == "tolerated":
        ft = run.baseline["failing_tests"]
        log(f"  [baseline] the base code builds, but {len(ft)} test(s) already fail "
            f"before any change by this run:")
        for t in ft[:_LIST_CAP]:
            log(f"    - {t}")
        if len(ft) > _LIST_CAP:
            log(f"    ... and {len(ft) - _LIST_CAP} more")
        log("  [baseline] the run continues. These tests are ignored at the build "
            "gate and listed in the pull request; any OTHER failing test stops it.")
        run.save(Path(harness_dir))
        return True
    _halt(run, rec, Path(harness_dir), log, reused)
    return False


def _halt(run, rec: dict, harness_dir: Path, log, reused: bool) -> None:
    import halt_gates as HG
    if str(rec.get("summary") or "").strip() == "BUILD FAILURE":
        # A result saved before the summary carried its cause.
        try:
            from validation import with_cause
            rec = dict(rec, summary=with_cause("BUILD FAILURE", rec.get("detail") or ""))
        except Exception:
            pass
    env = rec.get("status") == "environment"
    base = os.environ.get("HARNESS_BASE_BRANCH") or "the base branch"
    ft = list(rec.get("failing_tests") or [])
    tests = ""
    if ft:
        tests = "  Failing tests :\n" + "".join(f"    - {t}\n" for t in ft[:_LIST_CAP])
        if len(ft) > _LIST_CAP:
            tests += f"    ... and {len(ft) - _LIST_CAP} more\n"
    if env:
        title = "BASELINE BUILD: ENVIRONMENT FAILURE — HALTED"
        what = ("  The build could not run on the untouched base code, for a reason\n"
                "  outside the code. No AI phase has run and nothing was changed.\n")
        action = (f"  Action : {rec.get('env_hint') or 'Read the tail below and fix the build environment.'}\n"
                  f"  Then   : start the run again (a fresh run, or resume=true —\n"
                  f"           both start from the first phase).\n")
        run.halt_gate = HG.CONFIG_ERROR
        run.halt_detail = f"baseline environment ({rec.get('env_reason') or 'unknown'}): " \
                          f"{rec.get('summary')}"[:200]
    else:
        title = "BASELINE BUILD: EXISTING BUILD FAILURE — HALTED"
        what = (f"  The code on '{base}' does not build with the repo's test_command\n"
                "  BEFORE any change by the harness. This is not caused by the story\n"
                "  or by the harness. No AI phase has run and nothing was changed.\n")
        action = (f"  Action : fix the build on '{base}' (or correct test_command in\n"
                  "           .harness/config.yaml if the command is wrong), then start\n"
                  "           a FRESH run (resume = false). To use the harness without\n"
                  "           its build gate meanwhile, set runs_build: false.\n")
        run.halt_gate = HG.BASELINE_BUILD
        run.halt_detail = f"baseline: {rec.get('summary')}"[:200]
    msg = (f"\n  ================ {title} ================\n"
           + what
           + f"  Detail : {rec.get('summary')}\n"
           + (f"  Checked: {rec.get('checked_at')} (saved result for this base commit)\n"
              if reused else "")
           + tests + action
           + "  --- build output tail ---\n"
           + f"{rec.get('detail') or ''}\n"
           + "  ============================================================\n")
    log(msg)
    print(f"::error title=Baseline build failed::{rec.get('summary')} — "
          f"{'environment' if env else 'existing failure on the base branch'}; "
          f"no AI phase was run.")
    run.status = "halted"
    run.last_feedback = msg
    run.save(harness_dir)


# ---------------------------------------------------- used by the later phases
def known_failing(run) -> list:
    b = getattr(run, "baseline", None) or {}
    return list(b.get("failing_tests") or []) if b.get("status") == "tolerated" else []


def prompt_block(run, phase_id: str) -> str:
    """What a phase is told about tests that already fail. Empty when none do."""
    ft = known_failing(run)
    if not ft:
        return ""
    if phase_id in ("coding", "unit_testing", "code_review"):
        lines = ["", "",
                 "TESTS THAT ALREADY FAIL ON THE BASE BRANCH (before any change by this run):"]
        lines += [f"  - {t}" for t in ft[:_LIST_CAP]]
        if len(ft) > _LIST_CAP:
            lines.append(f"  ... and {len(ft) - _LIST_CAP} more")
        lines.append(
            "These failures existed before this story and the harness ignores them at "
            "its build gate. Do NOT edit or delete these tests, and do NOT change "
            "production code to make them pass, unless the story itself requires that "
            "change.")
        if phase_id == "code_review":
            lines.append("A production change made only to make one of these tests "
                         "pass is out of scope: report it as an issue.")
        return "\n".join(lines) + "\n"
    if phase_id in ("validation", "documentation", "raise_pr"):
        return ("\n\nBUILD NOTE: " + str(len(ft)) + " test(s) were already failing on the "
                "base branch before this change. The harness ignored them at its build "
                "gate; every other test passed. Do not describe the build as fully "
                "green: say that some tests were already failing before this change. "
                "Do not list them — the harness adds the list to the pull request "
                "itself.\n")
    return ""


def add_to_pr_body(repo_root, run) -> bool:
    """Add the already-failing tests to .harness/pr-body.md. Written by the
    harness, not the model, so the reviewer can rely on it."""
    ft = known_failing(run)
    f = Path(repo_root) / ".harness" / "pr-body.md"
    if not ft or not f.is_file():
        return False
    text = f.read_text(encoding="utf-8")
    if PR_MARKER in text:
        return False
    b = getattr(run, "baseline", None) or {}
    still = b.get("still_failing")
    lines = ["", "", PR_MARKER, "## Tests already failing before this change", "",
             (f"This test failed" if len(ft) == 1 else f"These {len(ft)} tests failed")
             + " on the base branch "
             f"(commit `{str(b.get('base_commit') or '')[:12] or 'unknown'}`) before the "
             "harness changed anything. The build gate ignored "
             + ("it" if len(ft) == 1 else "them") + "; every other test passed. "
             + ("It cannot catch a fault in the code it covers until it is fixed."
                if len(ft) == 1 else
                "They cannot catch a fault in the code they cover until they are fixed."),
             ""]
    for t in ft[:200]:
        mark = ""
        if isinstance(still, list) and t not in still:
            mark = " — passes after this change"
        lines.append(f"- `{t}`{mark}")
    if len(ft) > 200:
        lines.append(f"- ... and {len(ft) - 200} more")
    f.write_text(text.rstrip() + "\n".join(lines) + "\n", encoding="utf-8")
    return True
