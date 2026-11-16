"""
compile_check.py — compile the production code right after 'coding'.

WHY
No phase may run shell commands, so the coding agent cannot compile what it
writes. Until now the first compile was the build gate after unit_testing —
after code_review had reviewed, and unit_testing had written tests for, code
that did not compile. On OP018-6434 (2026-11-15) that happened twice
(SoapTotals2Translator.java:[163,72], AccumulatorsServiceImpl.java:[122,16]);
each cost a review and a test-writing phase before anyone knew.

WHAT IT DOES (only when runs_build is true, build mode)
After the coding phase finishes without error, the harness compiles the
production code:

  - compiles                 -> on to code_review, as before
  - compile error            -> back to 'coding' with the compiler's errors as
                                feedback (bounded by max_validation_retries per
                                run, then halt for a person)
  - environment failure      -> the same environment halt the build gate uses

THE COMMAND
`compile_command` in .harness/config.yaml when set ("off" disables the check).
When blank it is derived from a plain Maven test_command by swapping the
lifecycle goal for `compile`:

    mvn -U -T 1C -s $MAVEN_SETTINGS -e test   ->   mvn -U -T 1C -s $MAVEN_SETTINGS -e compile

A test_command that is not a single Maven call (npm/ng, a chained command)
cannot be derived: the check is skipped unless compile_command is set.
"""
from __future__ import annotations

import re
from pathlib import Path

_MVN = re.compile(r"(^|[\\/])mvnw?(\.cmd|\.bat)?$", re.IGNORECASE)
_TEST_GOALS = {"test", "verify", "package", "install", "test-compile",
               "integration-test", "deploy"}
_SHELL_OPS = ("&&", "||", ";", "|", ">", "<", "`", "$(")
_OFF = {"off", "none", "false", "no", "skip"}


def derive(test_command: str) -> str | None:
    """`mvn ... test` -> `mvn ... compile`. None when it cannot be derived."""
    cmd = (test_command or "").strip()
    if not cmd or any(op in cmd for op in _SHELL_OPS):
        return None
    toks = cmd.split()
    if not _MVN.search(toks[0].strip("\"'")):
        return None
    out, swapped = [toks[0]], False
    for t in toks[1:]:
        if t.startswith("-Dtest="):
            continue                       # a test filter means nothing to compile
        if t in _TEST_GOALS:
            if not swapped:
                out.append("compile")
                swapped = True
            continue
        out.append(t)
    return " ".join(out) if swapped else None


def command(cfg) -> str | None:
    explicit = (getattr(cfg, "compile_command", "") or "").strip()
    if explicit.lower() in _OFF:
        return None
    return explicit or derive(cfg.resolved_test_command())


def after_phase(sm, run, phase) -> bool:
    """Run the check after `phase`. True = the run was sent back or halted here
    (and saved); False = carry on to the normal transition."""
    from config import HarnessConfig, harness_runs_build
    import halt_gates as HG
    cfg = HarnessConfig.load(sm.harness_dir, repo_root=sm.repo_root)
    if phase.id != cfg.validation_loopback_phase:
        return False                                   # only after 'coding'
    if (getattr(run, "mode", None) or "build") != "build":
        return False
    if getattr(sm, "_validator", None) is not None:
        return False                                   # tests inject their own gate
    if not harness_runs_build(sm.harness_dir):
        return False
    if not (getattr(run, "changed_main_files", None) or []):
        return False                                   # nothing was written
    cmd = command(cfg)
    if not cmd:
        sm.log("  [compile] check skipped — no compile_command, and test_command is "
               "not a single Maven call to derive one from")
        return False

    from validation import run_validation
    sm.log("  [compile] compiling the production code before code review")
    vr = run_validation(sm.repo_root, sm.harness_dir, log=sm.log, compile_command=cmd)
    try:
        from protected_paths import restore_worktree
        restore_worktree(sm.repo_root, sm.harness_dir)
    except Exception:
        pass
    if vr.passed:
        sm.log("  [compile] the production code compiles")
        return False
    if getattr(vr, "failure_kind", None) == "environment":
        sm._halt_environment(run, phase, vr, vr.env_reason, vr.env_hint)
        return True
    if vr.exit_code < 0:
        # Timeout or the command could not be started: not a verdict on the
        # code. The build gate after unit_testing still stands.
        sm.log(f"  [compile] check did not complete ({vr.summary}) — continuing; the "
               "build gate after unit_testing still applies")
        return False

    run.compile_attempts = int(getattr(run, "compile_attempts", 0) or 0) + 1
    cap = cfg.max_validation_retries
    sm.log("  --- compile failure tail ---\n" + vr.output_tail)
    if run.compile_attempts <= cap:
        sm.log(f"  ! COMPILE_FAILED — the code '{phase.id}' wrote does not compile; "
               f"looping back to '{phase.id}' before code review "
               f"(attempt {run.compile_attempts}/{cap})")
        run.last_feedback = (
            "The production code you wrote does NOT COMPILE. Nothing has been "
            "reviewed or tested yet. Fix the compile errors below. Make the "
            "smallest change that compiles and still does what the story and the "
            "plan require; do not edit tests, and do not change the API contract "
            "to make it compile. Compiler output:\n" + vr.output_tail)
        run.fix_loop = "build"
        run.halt_detail = "compile: errors after coding"
        run.iterations[phase.id] = 0
        run.approvals[phase.id] = "rejected"
        run.current_phase = phase.id
        run.status = "running"
        run.save(sm.harness_dir)
        return True

    msg = (
        "\n  ================ COMPILE CHECK: HALTED ================\n"
        f"  The production code still does not compile after {cap} attempt(s)\n"
        f"  by '{phase.id}' to fix it. It has NOT gone to code review or testing.\n"
        f"  Command : {cmd}\n"
        "  Ways forward:\n"
        f"    1. Fix the compile error by hand on harness-wip/{run.feature_id},\n"
        "       push, then resume.\n"
        "    2. If the plan cannot be compiled as written (a missing class or\n"
        "       method the plan assumed), correct the story and start a fresh run.\n"
        "  --- compiler output ---\n"
        f"{vr.output_tail}\n"
        "  =======================================================\n")
    sm.log(msg)
    run.last_feedback = msg
    run.status = "halted"
    run.halt_gate = HG.TEST_BUILD
    run.halt_detail = "compile: errors after coding, retries exhausted"
    run.save(sm.harness_dir)
    return True
