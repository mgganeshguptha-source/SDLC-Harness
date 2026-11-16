"""
fix_scope.py — what 'coding' may edit when it is sent back to fix a red build.

WHY
On its first pass, coding may edit an existing file the plan did not list (it
is flagged for the reviewer). In a build-fix loop that freedom is dangerous: a
failing test's stack trace passes through many production files, and the model
"fixes" whichever one makes the test pass. On OP018-6434 (2026-11-15) coding,
sent back with failing tests, edited five production files the plan never
listed and removed a membership check on the way.

THE RULE (only when the build gate or the compile check sent coding back)
Coding may edit:
  - any file the plan lists,
  - any file this run had already changed before this pass,
  - a file the COMPILER names in a compile error in the failure it was given
    (the run changed a signature and a caller no longer compiles). A compile
    error names an exact file and line; a stack trace does not count.
An edit to any other existing production file is reverted and the run halts
for the developer. The reverted edits are kept as a patch.

Loops started by the code reviewer or the acceptance-criteria gate are not
restricted here: an independent phase named the need (petclinic PC-101 — the
reviewer asked for a fix in a repository class the plan had not listed), and
the reviewer sees the result again.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATCH_FILE = "unplanned-edits.patch"
_EXT = r"(?:java|kt|kts|groovy|scala|ts|tsx|js|jsx|mjs|html|scss|css|vue)"
# A source path followed by a compiler location:
#   Foo.java:[163,72]   (javac through Maven)
#   Foo.java:12: error  (plain javac)
#   a.ts:12:5           (tsc / Angular)
#   a.ts(12,5)          (tsc, older form)
# A stack frame "(Foo.java:138)" matches none of them.
_LOC = re.compile(
    r"((?:[A-Za-z]:)?[\w./\\$@+-]+\." + _EXT + r")"
    r"(?=:\[\d+,\d+\]|:\d+:\d+|\(\d+,\d+\)|:\d+:\s*error)")


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").strip()


def compile_error_files(text: str) -> set:
    """Paths the compiler names in error lines of a build output."""
    out = set()
    for line in (text or "").splitlines():
        if "error" not in line.lower():
            continue
        for m in _LOC.finditer(line):
            out.add(_norm(m.group(1)))
    return out


def _named(rel: str, error_paths: set) -> bool:
    r = _norm(rel)
    return any(e == r or e.endswith("/" + r) or r.endswith("/" + e) for e in error_paths)


def is_build_fix(run, phase_id: str) -> bool:
    """Is this pass a redo ordered by the build gate or the compile check?"""
    return (getattr(run, "fix_loop", "") == "build"
            and (getattr(run, "approvals", None) or {}).get(phase_id) == "rejected"
            and bool(getattr(run, "last_feedback", None)))


def blocked_edits(unplanned_modified: list, changed_before: set, feedback: str) -> list:
    """The unplanned edits this pass may not make.

    unplanned_modified: existing production files edited this pass that the
        plan does not list (the scope gate's 'modified' additions).
    changed_before: production files this run had changed before this pass.
    feedback: the failure text coding was sent back with."""
    before = {_norm(p) for p in (changed_before or ())}
    named = compile_error_files(feedback)
    return sorted(f for f in (_norm(x) for x in unplanned_modified)
                  if f not in before and not _named(f, named))


def revert(repo_root, harness_dir, files: list, log=print) -> bool:
    """Save the edits as a patch, then put the files back as they were at the
    last commit (this run had not changed them, so that is their state before
    this pass). True when every file was restored."""
    ok = True
    try:
        d = subprocess.run(["git", "-C", str(repo_root), "diff", "HEAD", "--", *files],
                           capture_output=True, text=True, timeout=60)
        if d.returncode == 0 and d.stdout.strip():
            (Path(harness_dir) / PATCH_FILE).write_text(d.stdout, encoding="utf-8")
        r = subprocess.run(["git", "-C", str(repo_root), "checkout", "HEAD", "--", *files],
                           capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        if not ok:
            log(f"  [fix-scope] could not restore the files: {(r.stderr or '').strip()[:200]}")
    except Exception as e:
        ok = False
        log(f"  [fix-scope] could not restore the files ({type(e).__name__})")
    return ok


def halt_message(phase_id: str, feature_id: str, blocked: list, reverted: bool,
                 allowed_before: list) -> str:
    kept = (f"  The edits were REVERTED. They are saved as .harness/{PATCH_FILE}\n"
            f"  (also in this run's audit folder) so you can see what was tried.\n"
            if reverted else
            "  The edits could NOT be reverted automatically — they are still in\n"
            "  the working tree. Review them before anything else.\n")
    return (
        "\n  ============ FIX LOOP: UNPLANNED EDIT — HALTED ============\n"
        f"  '{phase_id}' was sent back to fix a red build and edited production\n"
        "  file(s) that the plan does not list and this run had not touched:\n"
        + "".join(f"    - {b}\n" for b in blocked)
        + "  A failing test passing through a file is not a reason to change it.\n"
        "  In a build-fix loop coding may edit only the plan's files, files this\n"
        "  run already changed, and a file the compiler names in a compile error.\n"
        + kept
        + "  Files this run had changed before this pass:\n"
        + ("".join(f"    - {a}\n" for a in allowed_before[:20]) or "    (none)\n")
        + "  Ways forward:\n"
        f"    1. The edit is right: make it by hand on harness-wip/{feature_id}\n"
        f"       (git apply the patch), push, then resume.\n"
        "    2. The failing test is wrong, or has nothing to do with the story:\n"
        f"       fix or remove it on harness-wip/{feature_id}, push, then resume.\n"
        "    3. The story really needs these files: say so in the story and start\n"
        "       a fresh run, so the plan lists them.\n"
        "  ===========================================================\n")


def prompt_block(run, phase_id: str) -> str:
    """Tell coding the rule before it spends the pass. Empty outside a
    build-fix loop."""
    if not is_build_fix(run, phase_id):
        return ""
    changed = sorted(getattr(run, "changed_main_files", None) or [])
    lines = ["", "",
             "BUILD-FIX LOOP — WHICH FILES YOU MAY EDIT IN THIS PASS:",
             "  - the files the approved plan lists;",
             "  - the files this run has already changed:"]
    lines += [f"      {c}" for c in changed[:30]] or ["      (none yet)"]
    lines += [
        "  - a file the COMPILER names in a compile error in the failure below.",
        "Do NOT edit any other existing production file. A failing test whose "
        "stack trace passes through a file is not a reason to change that file. "
        "If the failure cannot be fixed within these files, do not widen the "
        "change: leave the other files alone and say in one line starting "
        "'OUT OF SCOPE:' which file would need to change and why. The harness "
        "reverts an edit outside this list and stops the run for the developer."]
    return "\n".join(lines) + "\n"
