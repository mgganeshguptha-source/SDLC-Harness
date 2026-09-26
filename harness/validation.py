"""
validation.py — the deterministic validation gate.

THE HARNESS runs the tests, not the agent. After the unit_testing phase, the state
machine calls run_validation(); it shells out to the configured test command FROM
THE HARNESS (this is allowed — it's our trusted code, not the model), captures the
exit code, and returns pass/fail. A non-zero exit => VALIDATION_FAILED => the run
HALTS before documentation/PR.

This is the interlock that makes "you cannot ship red tests" a guarantee rather
than a hope: the green run is verified by code, as a precondition to proceeding.
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config import HarnessConfig


@dataclass
class ValidationResult:
    passed: bool
    exit_code: int
    summary: str
    output_tail: str
    # Why validation failed, so the state machine can route correctly:
    #   None       -> passed
    #   "test"     -> tests are RED  => loop back to coding
    #   "coverage" -> tests pass but per-change coverage below threshold
    #                 => loop back to unit_testing
    failure_kind: str | None = None
    # When failure_kind == "environment": which kind of environment problem, and
    # what a human should do about it. The state machine prints these in the halt
    # message so the operator is not told to chmod mvnw for a network problem.
    #   env_reason: "toolchain" | "dependency" | "network_auth" | "unattributable"
    env_reason: str | None = None
    env_hint: str = ""
    # Coverage detail (populated when the coverage gate ran), for messaging.
    coverage_pct: float | None = None
    coverage_target: float | None = None
    coverage_classes: list = field(default_factory=list)   # classes measured
    coverage_covered: int = 0
    coverage_missed: int = 0


def _java_file_to_class_key(rel_path: str) -> tuple[str, str] | None:
    """Map a repo-relative .java source path to (package, ClassName) as JaCoCo
    reports them in its CSV.

    'src/main/java/org/springframework/samples/petclinic/owner/Owner.java'
      -> ('org.springframework.samples.petclinic.owner', 'Owner')
    Returns None for non-source or unparseable paths.
    """
    p = rel_path.replace("\\", "/").strip()
    if not p.endswith(".java"):
        return None
    # locate the source root marker
    for marker in ("src/main/java/", "src/test/java/", "src/main/kotlin/"):
        i = p.find(marker)
        if i != -1:
            pkgpath = p[i + len(marker):]
            break
    else:
        # no recognizable source root; fall back to basename only
        pkgpath = p.rsplit("/", 1)[-1]
    cls = pkgpath.rsplit("/", 1)[-1][:-len(".java")]
    pkg_dir = pkgpath[: -len(pkgpath.rsplit("/", 1)[-1])].rstrip("/")
    package = pkg_dir.replace("/", ".")
    return (package, cls)


def _parse_changed_coverage(csv_path: Path, metric: str,
                            changed_files: list) -> tuple[float | None, int, int, list]:
    """Per-change coverage: compute `metric` coverage over ONLY the JaCoCo rows
    whose (PACKAGE, CLASS) match the changed source files.

    Returns (percent | None, covered, missed, measured_class_labels).
    percent is None if the report is missing/unreadable OR none of the changed
    classes appear in the report (e.g. brand-new class with no test touching it
    still appears with 0 covered; truly absent => None so caller can decide).
    """
    if not csv_path.exists():
        return (None, 0, 0, [])

    # Build the set of (package, class) we care about. JaCoCo emits nested/inner
    # classes as 'Owner.Inner' or 'Owner$1'; match the top-level class as a prefix.
    wanted = set()
    for f in changed_files or []:
        key = _java_file_to_class_key(f)
        if key:
            wanted.add(key)
    if not wanted:
        return (None, 0, 0, [])

    try:
        import csv as _csv
        covered = missed = 0
        measured = []
        matched_any = False
        with csv_path.open(encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                pkg = row.get("PACKAGE", "")
                cls = row.get("CLASS", "")
                # top-level class name (strip inner-class suffixes)
                top = cls.split("$", 1)[0].split(".", 1)[0]
                if (pkg, top) in wanted:
                    matched_any = True
                    m = int(row[f"{metric}_MISSED"])
                    c = int(row[f"{metric}_COVERED"])
                    missed += m
                    covered += c
                    measured.append(f"{pkg}.{cls}")
        if not matched_any:
            return (None, 0, 0, [])
        total = covered + missed
        if total == 0:
            # class(es) matched but have zero of this metric (e.g. an interface).
            # Treat as 100% — nothing to cover — rather than a failure.
            return (100.0, covered, missed, measured)
        return (100.0 * covered / total, covered, missed, measured)
    except Exception:
        return (None, 0, 0, [])


def _parse_coverage(csv_path: Path, metric: str) -> float | None:
    """Global coverage across ALL rows (legacy 'global' scope)."""
    if not csv_path.exists():
        return None
    try:
        import csv as _csv
        covered = missed = 0
        with csv_path.open(encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                missed += int(row[f"{metric}_MISSED"])
                covered += int(row[f"{metric}_COVERED"])
        total = covered + missed
        if total == 0:
            return None
        return 100.0 * covered / total
    except Exception:
        return None


def _surefire_failures(repo_root: Path, log=print, max_chars: int = 4000) -> str:
    """Extract the failing-test detail from surefire .txt reports.

    Module-aware via rglob, so it works for single-module and aggregator repos
    alike. Only reports that actually contain failures/errors are included, and
    the whole thing is capped so a large suite cannot flood the feedback prompt.
    """
    try:
        reports = sorted(repo_root.rglob("target/surefire-reports/*.txt"))
    except Exception:
        return ""
    chunks: list[str] = []
    for rp in reports:
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Surefire writes a header line like:
        #   Tests run: 5, Failures: 1, Errors: 0, Skipped: 0
        # Skip clean reports.
        if "Failures: 0, Errors: 0" in text:
            continue
        if "Tests run:" not in text:
            continue
        chunks.append(f"### {rp.name}\n{text.strip()}")
    if not chunks:
        return ""
    joined = "\n\n".join(chunks)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n... [truncated by harness]"
    log(f"  [harness] attached failing-test detail from {len(chunks)} surefire report(s)")
    return joined


# ---- ENVIRONMENT SIGNATURES ----
# Failures that no coding or unit_testing phase can fix, because the cause is not
# in src/main or src/test. Each group has its own remedy, printed in the halt
# message. All matching is on lower-cased output.
#
# DEPENDENCY and NETWORK/AUTH signatures are specific to Maven's resolver and
# never appear in a compile error or a red test, so they are trusted on sight.
# Observed at BCBSM (run 36233435904): an internal parent POM
# (mem-starter-parent) that only the corporate artifact repository serves.
# Before this list existed the failure was routed to 'coding' and
# 'unit_testing' as a code defect, and burned the retry budget on a problem
# neither phase could touch.
_DEPENDENCY_SIGNATURES = (
    "non-resolvable parent pom",
    "non-resolvable import pom",
    "unresolvablemodelexception",
    "could not resolve dependencies",
    "could not transfer artifact",
    "failed to read artifact descriptor",
    "or one of its dependencies could not be resolved",   # plugin resolution
    "could not find artifact",
    "was cached in the local repository, resolution will not be reattempted",
)
_NETWORK_AUTH_SIGNATURES = (
    "unknownhostexception",
    "unknown host",
    "connection refused",
    "connect timed out",
    "connection timed out",
    "no route to host",
    "pkix path building failed",
    "unable to find valid certification path",
    "status code: 401",
    "status code: 403",
    "401 unauthorized",
    "403 forbidden",
    "not authorized",
)
# TOOLCHAIN signatures mean the build tool itself never started. Some of these
# strings are generic ("no such file or directory", "permission denied") and can
# legitimately appear in a TEST's own output, so they are only trusted when the
# build lifecycle shows no sign of having run (see _lifecycle_started).
_TOOLCHAIN_SIGNATURES = (
    "mvnw: permission denied",
    "mvnw: not found",
    "./mvnw: 1: ",                       # sh wrapper error prefix on a broken mvnw
    "permission denied",
    "no such file or directory",
    "command not found",
    "unable to access jarfile",
    "could not create the java virtual machine",
    "no java virtual machine",
    "java_home is not defined",
    "could not find or load main class",
    "mavenwrappermain",
    "no compiler is provided in this environment",
)

_HINTS = {
    "toolchain": (
        "The build tool could not start. Check that the Maven wrapper is complete\n"
        "           (mvnw plus .mvn/wrapper/), executable, and uses LF line endings,\n"
        "           and that a JDK is on the runner. The harness workflow makes\n"
        "           mvnw executable and falls back to the runner's mvn when\n"
        "           .mvn/wrapper/ is missing — if this still fails, the wrapper\n"
        "           or JDK setup on the runner needs attention."),
    "dependency": (
        "Maven could not download a parent POM, dependency or plugin. The code\n"
        "           was never compiled, so no phase can fix this. Usually the\n"
        "           artifact lives in a private/internal repository the runner\n"
        "           cannot reach. Fix one of:\n"
        "             - give the runner a settings.xml with that repository and\n"
        "               read credentials (secrets), if it is reachable from here;\n"
        "             - run the harness on a self-hosted runner inside the network\n"
        "               that normally builds this repo;\n"
        "             - or, if a NEW dependency is the cause, add it by hand.\n"
        "           Then resume from the phase that halted."),
    "network_auth": (
        "Maven could not reach, or was refused by, a repository (host lookup,\n"
        "           connection, TLS certificate, or 401/403). Check the runner's\n"
        "           network access, proxy/certificate setup, and repository\n"
        "           credentials, then resume."),
    "unattributable": (
        "The build failed the same way again after a phase changed the code, and\n"
        "           the failure names no source file, no test and no compile error —\n"
        "           so the code phases cannot be what is wrong. Read the tail above,\n"
        "           fix the build environment, then resume."),
}


def _lifecycle_started(low: str) -> bool:
    """True when Maven clearly got far enough to build or test something.

    Used to keep generic launcher strings ("no such file or directory") from
    misclassifying a test that failed on a missing file as an environment fault.
    """
    return ("tests run:" in low
            or "compilation error" in low
            or "compilation failure" in low
            or "t e s t s" in low)


def classify_environment_failure(exit_code: int, output: str) -> str | None:
    """Return the environment reason when the build failed for a cause no code
    phase can fix, else None.

    Order matters: dependency and network/auth signatures are specific and win;
    generic toolchain strings only count when the lifecycle never started.
    """
    # 126 = found but not executable (e.g. ./mvnw without the +x bit).
    # 127 = command not found (missing wrapper / mvn not on PATH).
    if exit_code in (126, 127):
        return "toolchain"
    low = (output or "").lower()
    if any(s in low for s in _DEPENDENCY_SIGNATURES):
        return "dependency"
    if any(s in low for s in _NETWORK_AUTH_SIGNATURES) and not _lifecycle_started(low):
        return "network_auth"
    if any(s in low for s in _TOOLCHAIN_SIGNATURES) and not _lifecycle_started(low):
        return "toolchain"
    return None


def environment_hint(reason: str | None) -> str:
    return _HINTS.get(reason or "", "")


def is_code_attributable(output: str) -> bool:
    """True when a failed build points at code the phases own: a source path, a
    compile error, or a test result. A failure with none of these cannot be fixed
    by editing src/main or src/test — used by the state machine to stop repeating
    loopbacks against an unrecognised environment fault."""
    low = (output or "").lower()
    return (_lifecycle_started(low)
            or "/src/main/" in low or "\\src\\main\\" in low
            or "/src/test/" in low or "\\src\\test\\" in low
            or ".java:[" in low)


def _is_environment_failure(exit_code: int, output: str) -> bool:
    """Backward-compatible boolean wrapper around classify_environment_failure."""
    return classify_environment_failure(exit_code, output) is not None


def run_validation(repo_root: Path, harness_dir: Path, log=print,
                   changed_files: list | None = None) -> ValidationResult:
    cfg = HarnessConfig.load(harness_dir)

    # ---- DETERMINISTIC PRE-STEP: normalize formatting ----
    # The harness applies the project's required format (spring-javaformat) before
    # validating. This is a fixed, known goal run by trusted harness code — not the
    # agent, and not arbitrary execution. It turns a mechanical "format violation"
    # into a non-issue so validation tests true correctness, not whitespace.
    if cfg.pre_validation_command:
        log(f"  [harness] formatting: {cfg.pre_validation_command}")
        try:
            fmt = subprocess.run(
                cfg.pre_validation_command,
                cwd=str(repo_root), shell=True, capture_output=True,
                text=True, timeout=cfg.test_timeout,
            )
            if fmt.returncode == 0:
                log("  [harness] formatting applied (or already clean)")
            else:
                # Distinguish "this repo has no formatter configured" from "the
                # formatter ran and failed". The first is benign and extremely
                # common — the sample repo has no spring-javaformat plugin, so
                # every run logged "formatting step returned exit 1", which reads
                # like a failure to anyone new and sent joiners hunting a
                # non-problem. The second is worth seeing.
                _out = (fmt.stdout or "") + (fmt.stderr or "")
                _missing = (
                    "No plugin found for prefix" in _out
                    or "Could not find goal" in _out
                    or "does not exist or no valid version" in _out
                    or "Unknown lifecycle phase" in _out
                )
                if _missing:
                    log("  [harness] formatting skipped — no formatter plugin "
                        "configured in this repo (this is fine)")
                    log('             set pre_validation_command: "" in '
                        '.harness/config.yaml to silence this')
                else:
                    # Non-fatal: log and continue; validation will catch real problems.
                    log(f"  [harness] formatting step returned exit {fmt.returncode} "
                        f"(continuing)")
                    _tail = "\n".join(_out.strip().splitlines()[-5:])
                    if _tail:
                        log(f"             {_tail}")
        except Exception as e:
            log(f"  [harness] formatting step error: {type(e).__name__}: {e} (continuing)")

    cmd = cfg.resolved_test_command()
    log(f"  [harness] running validation: {cmd}")
    log(f"  [harness] cwd: {repo_root}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            shell=True,                # needed for mvnw.cmd on Windows
            capture_output=True,
            text=True,
            timeout=cfg.test_timeout,
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(False, -1, "TIMEOUT", f"test run exceeded {cfg.test_timeout}s")
    except Exception as e:
        return ValidationResult(False, -2, f"ERROR: {type(e).__name__}: {e}", "")

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = "\n".join(out.splitlines()[-25:])  # last lines hold the BUILD result

    # Maven's tail on a TEST failure says only "See .../surefire-reports for the
    # individual test results" — the actual failing test, assertion and stack trace
    # live in files on disk. Without them the loopback feedback carries no
    # diagnosis, so the fixing phase is reduced to guessing: in run 31257053514 it
    # burned every retry speculating (e.g. "switching to @Slf4j avoids missing
    # Log4j classes at test runtime") because it never saw the actual error.
    # Pull the failure detail out of the reports and attach it to the feedback.
    if proc.returncode != 0 and "surefire-reports" in out:
        detail = _surefire_failures(repo_root, log=log)
        if detail:
            tail = tail + "\n\n--- FAILING TEST DETAIL (from surefire-reports) ---\n" + detail

    passed = proc.returncode == 0
    failure_kind = None if passed else "test"

    # ---- ENVIRONMENT FAILURE: the build never ran ----
    # exit 126/127 or a launcher error means the tool could not start — the wrapper
    # isn't executable, isn't found, or the JVM couldn't launch. No code or test
    # phase can fix this, so mark it distinctly; the state machine halts on it
    # instead of looping coding/unit_testing against an unfixable error.
    _env = None if passed else classify_environment_failure(proc.returncode, out)
    if _env:
        failure_kind = "environment"
        _labels = {
            "toolchain": "build tool could not start",
            "dependency": "a parent POM, dependency or plugin could not be resolved",
            "network_auth": "a repository could not be reached or refused access",
        }
        summary = (f"ENVIRONMENT FAILURE (exit {proc.returncode}) — "
                   f"{_labels.get(_env, 'build did not run')}")
        log(f"  ! {summary}")
        log("    This is an environment problem, not a code problem — no phase "
            "can fix it, so the harness will halt instead of looping.")
        report = harness_dir / "validation-report.txt"
        try:
            report.write_text(
                f"command: {cmd}\nexit_code: {proc.returncode}\n"
                f"summary: {summary}\nenvironment_reason: {_env}\n\n--- tail ---\n"
                + tail.replace("http://", "hxxp://") + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        return ValidationResult(
            False, proc.returncode, summary, tail, failure_kind="environment",
            env_reason=_env, env_hint=environment_hint(_env),
        )

    # Maven prints BUILD SUCCESS / BUILD FAILURE; use exit code as source of truth,
    # the text scan is only for a friendlier summary line.
    if "BUILD SUCCESS" in out:
        summary = "BUILD SUCCESS"
    elif "BUILD FAILURE" in out:
        summary = "BUILD FAILURE"
    else:
        summary = f"exit {proc.returncode}"

    # ---- COVERAGE GATE (only if tests passed and a threshold is set) ----
    coverage_note = ""
    cov_pct = None
    cov_covered = cov_missed = 0
    cov_measured: list = []
    if passed and cfg.min_coverage > 0:
        log(f"  [harness] coverage: {cfg.coverage_command}")
        try:
            subprocess.run(cfg.coverage_command, cwd=str(repo_root), shell=True,
                           capture_output=True, text=True, timeout=cfg.test_timeout)
        except Exception as e:
            log(f"  [harness] coverage command error: {e} (continuing to parse if report exists)")

        # Locate the JaCoCo CSV module-aware. `coverage_csv` is configured
        # MODULE-relative (target/site/jacoco/...), which equals the repo root only
        # in a single-module repo. In an aggregator repo the report lives under
        # <app-module>/, so joining it to repo_root finds nothing and the gate
        # reports "unmeasurable" while the tests are actually green.
        csv_path = cfg.resolved_coverage_csv(repo_root)
        if csv_path is None:
            # Nothing found anywhere — keep a concrete path for the message so the
            # failure names what was looked for rather than a None.
            csv_path = repo_root / cfg.coverage_csv
        else:
            try:
                log(f"  [harness] coverage report: {csv_path.relative_to(repo_root)}")
            except Exception:
                log(f"  [harness] coverage report: {csv_path}")
        if cfg.coverage_scope == "changed":
            # PER-CHANGE coverage: only the classes the coding phase wrote this run.
            log(f"  [harness] coverage scope: changed classes = "
                f"{changed_files if changed_files else '(none recorded)'}")
            cov_pct, cov_covered, cov_missed, cov_measured = _parse_changed_coverage(
                csv_path, cfg.coverage_metric, changed_files or [])
        else:
            cov_pct = _parse_coverage(csv_path, cfg.coverage_metric)

        if cov_pct is None:
            # Can't measure -> treat as a gate failure (don't silently pass). For
            # "changed" scope this also fires when none of the changed classes
            # appear in the report (misconfigured JaCoCo, or no changed source).
            passed = False
            failure_kind = "coverage"
            summary = "COVERAGE UNREADABLE"
            if cfg.coverage_scope == "changed":
                coverage_note = (
                    f"Could not measure {cfg.coverage_metric} coverage for the changed "
                    f"class(es) {changed_files or '[]'} from {cfg.coverage_csv}. "
                    f"Check that JaCoCo (jacoco-maven-plugin) is configured and that the "
                    f"changed files are main source.")
            else:
                coverage_note = f"Could not read {cfg.coverage_metric} coverage from {cfg.coverage_csv}"
            log("  ! " + coverage_note)
        elif cov_pct < cfg.min_coverage:
            passed = False
            failure_kind = "coverage"
            scope_lbl = "changed-class" if cfg.coverage_scope == "changed" else "global"
            summary = f"COVERAGE {cov_pct:.1f}% < {cfg.min_coverage:.1f}% ({scope_lbl})"
            coverage_note = (
                f"{scope_lbl} {cfg.coverage_metric} coverage {cov_pct:.1f}% is below the "
                f"required {cfg.min_coverage:.1f}% "
                f"(covered={cov_covered}, missed={cov_missed}; "
                f"measured: {', '.join(cov_measured) if cov_measured else 'n/a'})")
            log("  ! " + coverage_note)
        else:
            scope_lbl = "changed-class" if cfg.coverage_scope == "changed" else "global"
            coverage_note = (f"{scope_lbl} {cfg.coverage_metric} coverage {cov_pct:.1f}% "
                             f"(>= {cfg.min_coverage:.1f}%)")
            log(f"  [harness] {coverage_note}")

    # Write a validation report into the workspace (auditable artifact).
    # NOTE: petclinic's nohttp checkstyle scans the whole tree including .harness/,
    # and would flag any literal http:// URL we capture from Maven output. Neutralize
    # such URLs in the persisted report so our own audit file can't fail the build.
    report = harness_dir / "validation-report.txt"
    safe_tail = tail.replace("http://", "hxxp://")
    cov_line = f"coverage: {coverage_note}\n" if coverage_note else ""
    try:
        report.write_text(
            f"command: {cmd}\nexit_code: {proc.returncode}\nsummary: {summary}\n{cov_line}\n--- tail ---\n{safe_tail}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    final_tail = tail if not coverage_note else (tail + "\n\nCOVERAGE: " + coverage_note)
    return ValidationResult(
        passed, proc.returncode, summary, final_tail,
        failure_kind=failure_kind,
        coverage_pct=cov_pct,
        coverage_target=(cfg.min_coverage if cfg.min_coverage > 0 else None),
        coverage_classes=cov_measured,
        coverage_covered=cov_covered,
        coverage_missed=cov_missed,
    )
