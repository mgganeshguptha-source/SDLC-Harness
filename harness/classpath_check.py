"""
classpath_check.py — is a flagged class REALLY missing?

WHY THIS EXISTS
plan_check.check_external() halts the run when the plan's "External Classes"
table marks a row UNKNOWN or NEW_DEPENDENCY. That table is written by the
planning model, and the model reads the module's own pom.xml. It does not see:

  - dependencies inherited from a parent POM's <dependencies>
  - dependencies pulled in transitively by another dependency
  - anything managed and declared further up the parent chain

On OP018-6434 (BCBSM, 2026-11-19) the plan listed five CXF classes as UNKNOWN.
The CXF artifacts were declared in the repo's own root POM, which the
application module inherits, so every one of those classes was on the compile
classpath. The run halted twice for a person who had nothing to fix. The same
false halt waits for any service that gets a library from mem-starter-parent.

WHAT THIS DOES
Before the halt, ask Maven for the module's real compile classpath and look
inside the jars. A flagged class that is actually there is cleared and the run
carries on. A class that is genuinely absent still halts, with the classpath
evidence added so the message says "checked, not there" rather than "the model
could not find it".

This never makes the gate weaker: it only overturns the model's guess with a
fact. Nothing is cleared when the classpath cannot be resolved.

COPY_FROM_REFERENCE rows are left alone — they ask for source files to be
copied, which is not a question about the classpath.

WHEN IT RUNS
Only with runs_build true (otherwise the runner cannot run Maven at all) and
only when a row was already flagged, so a clean run pays nothing for it.

INTERFACE
    verify(flagged, repo_root, cfg, log) -> VerifyResult
        .still_flagged : list[ExternalClass]  (truthy => halt)
        .cleared       : list[str]            class names found on the classpath
        .checked       : bool                 the classpath was resolved
        .note          : str                  why not, when it was not
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Rows that are a claim about the classpath, and so can be checked. A
# COPY_FROM_REFERENCE row asks for files to be copied and is not one.
CHECKABLE = ("UNKNOWN", "NEW_DEPENDENCY")

_MVN = re.compile(r"(^|[\\/])mvnw?(\.cmd|\.bat)?$", re.IGNORECASE)
_SHELL_OPS = ("&&", "||", ";", "|", ">", "<", "`", "$(")
# Goals and flags of the test command that must not come along.
_DROP_GOALS = {"test", "verify", "package", "install", "test-compile",
               "integration-test", "deploy", "compile", "clean"}
_DROP_FLAGS = ("-T", "-Dtest=", "-Dit.test=", "-DfailIfNoTests")
MAX_SECONDS = 600


@dataclass
class VerifyResult:
    still_flagged: List = field(default_factory=list)
    cleared: List[str] = field(default_factory=list)
    checked: bool = False
    note: str = ""


def derive_command(test_command: str, module: str, out_file: Path) -> Optional[str]:
    """A `dependency:build-classpath` call built from the service's own test
    command, so it keeps that command's settings file and flags. None when the
    test command is not a single plain Maven call."""
    cmd = (test_command or "").strip()
    if not cmd or any(op in cmd for op in _SHELL_OPS):
        return None
    toks = cmd.split()
    if not _MVN.search(toks[0].strip("\"'")):
        return None
    out = [toks[0]]
    skip_next = False
    for t in toks[1:]:
        if skip_next:
            skip_next = False
            continue
        if t in ("-T", "-pl", "-Dtest"):     # flag whose value is the next token
            skip_next = True
            continue
        if t in _DROP_GOALS or t.startswith(_DROP_FLAGS):
            continue
        out.append(t)
    out.append("-q")
    if module:
        out += ["-pl", module]
    out += ["org.apache.maven.plugins:maven-dependency-plugin:build-classpath",
            f"-Dmdep.outputFile={out_file}", "-Dmdep.includeScope=test"]
    return " ".join(out)


def _entry_names(fqcn: str) -> List[str]:
    """Jar entry names a fully-qualified class could be stored under."""
    name = (fqcn or "").strip().strip("`").replace("/", ".")
    if not name or not re.match(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$", name):
        return []
    parts = name.split(".")
    names = ["/".join(parts) + ".class"]
    if len(parts) > 2:
        # A nested class is stored as Outer$Inner.class.
        names.append("/".join(parts[:-2] + [parts[-2] + "$" + parts[-1]]) + ".class")
    return names


def classpath_entries(repo_root: Path, cfg, log=print) -> Optional[List[Path]]:
    """The module's resolved compile+test classpath, or None when Maven could
    not produce it (no settings, plugin not in the store, build failure)."""
    module = (getattr(cfg, "target_module", "") or "").strip()
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "cp.txt"
        cmd = derive_command(cfg.resolved_test_command(), module, out_file)
        if not cmd:
            return None
        try:
            proc = subprocess.run(cmd, cwd=str(repo_root), shell=True,
                                  capture_output=True, text=True,
                                  timeout=min(getattr(cfg, "test_timeout", MAX_SECONDS),
                                              MAX_SECONDS),
                                  env=dict(os.environ))
        except Exception as e:
            log(f"  [harness] classpath check: {type(e).__name__}")
            return None
        if proc.returncode != 0 or not out_file.is_file():
            first = next((ln.strip() for ln in (proc.stdout or "").splitlines()
                          + (proc.stderr or "").splitlines()
                          if ln.strip().startswith("[ERROR]")), "")
            log(f"  [harness] classpath check: Maven could not list the classpath"
                + (f" — {first[:160]}" if first else ""))
            return None
        raw = out_file.read_text(encoding="utf-8", errors="replace").strip()
    seen, entries = set(), []
    for part in raw.replace("\n", os.pathsep).split(os.pathsep):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            entries.append(Path(part))
    return entries


def present(entries: List[Path], fqcn: str) -> bool:
    """True when the class is in one of the classpath entries."""
    wanted = _entry_names(fqcn)
    if not wanted:
        return False
    for entry in entries:
        try:
            if entry.is_dir():
                if any((entry / w).is_file() for w in wanted):
                    return True
            elif entry.is_file():
                with zipfile.ZipFile(entry) as z:
                    names = set(z.namelist())
                    if any(w in names for w in wanted):
                        return True
        except Exception:
            continue
    return False


def verify(flagged: List, repo_root: Path, cfg, log=print) -> VerifyResult:
    """Clear every flagged class that is really on the classpath."""
    res = VerifyResult(still_flagged=list(flagged))
    checkable = [f for f in flagged if f.source in CHECKABLE]
    if not checkable:
        res.note = "no row makes a claim about the classpath"
        return res
    from config import harness_runs_build
    if not harness_runs_build(repo_root / ".harness"):
        res.note = "runs_build is false, so the classpath cannot be resolved here"
        log(f"  [harness] classpath check skipped: {res.note}")
        return res
    log(f"  [harness] classpath check: asking Maven for the real classpath of "
        f"{len(checkable)} flagged class(es) ...")
    entries = classpath_entries(repo_root, cfg, log=log)
    if entries is None:
        res.note = "the classpath could not be resolved"
        return res
    res.checked = True
    log(f"  [harness] classpath check: {len(entries)} entries on the classpath")
    kept = []
    for f in flagged:
        if f.source in CHECKABLE and present(entries, f.name):
            res.cleared.append(f.name)
            log(f"  [harness] classpath check: {f.name} IS on the classpath "
                f"(inherited or transitive) — not a missing dependency")
        else:
            kept.append(f)
    res.still_flagged = kept
    if res.cleared and not kept:
        log(f"  [harness] classpath check: every flagged class resolves; "
            f"the plan's table was wrong and the run continues")
    return res


def evidence_note(res: VerifyResult) -> str:
    """A line for the halt message saying what the classpath check found."""
    if res.checked:
        n = len(res.still_flagged)
        line = (f"The harness resolved the module's real classpath and confirmed "
                f"{n} class(es) are not on it (inherited and transitive "
                f"dependencies included).")
        if res.cleared:
            line += (f" {len(res.cleared)} other class(es) the plan called missing "
                     f"were found and ignored: {', '.join(res.cleared)}.")
        return line
    return (f"The harness could not resolve the classpath to confirm this "
            f"({res.note}), so this is the planning model's finding alone.")
