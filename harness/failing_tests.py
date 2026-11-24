"""
failing_tests.py — tests that already fail on the base branch, after the
baseline check (baseline.py) has listed them: which ones belong to the code a
run works on, how they are triaged, the guard that stops a run from quietly
changing them, and the opt-in repair of stale ones in unit-test mode.

WHY
A test that fails on the base branch either has a stale expectation (the code
changed on purpose, the test was never updated) or is catching a real bug.
Changing the test is right in the first case and hides the bug in the second,
and a model cannot reliably tell the two apart. So by default nothing touches
these tests: the prompt says so and this module enforces it. They are listed
in every pull request, and in unit-test mode the test author classifies the
ones in its batch, so they stay visible instead of sitting red for ever.

THE RULES
  unit-test mode
    - triage: every already-failing test of a class in the batch gets one line
      in .harness/failing-tests-triage.md:
          - <class>#<method>: STALE | DEFECT | FLAKY — <reason>
    - guard: an edit to ANY already-failing test method is put back as it was
      before the phase (the method body, its annotations included); deleting
      one sends unit_testing back to restore it.
    - repair (opt-in, unit_test.repair_failing: true, default false): a test of
      a class in the batch that the triage marks STALE may be edited — kept,
      not deleted, under the same name. Its classes go into the batch first.
      A repaired test is no longer ignored at the build gate: it must pass.
      The reviewer sees the old and new method side by side; the PR lists it.
  build mode
    - report only: edits by coding or unit_testing to an already-failing test
      are recorded and marked in the PR list. A story may legitimately change
      behaviour that such a test covers, so nothing is reverted.

Java (surefire ids '<class>#<method>') only: a build without surefire reports
(Angular/Karma, Gradle) cannot list already-failing tests at all — the baseline
check halts there instead (baseline.py).
"""
from __future__ import annotations

import re
from pathlib import Path

TRIAGE_FILE = ".harness/failing-tests-triage.md"
VERDICTS = ("STALE", "DEFECT", "FLAKY")
_TEST_SUFFIXES = ("Tests", "Test", "IT", "TestCase")
_TEXT_CAP = 6000
_LIST_CAP = 40


# ------------------------------------------------------------------- ids
def split_id(test_id: str) -> tuple:
    """('com.x.FooTest', 'methodName') — the method part cut to its leading
    identifier, so 'shouldMap(String)[1]' and 'shouldMap{...}' name the method."""
    cls, _, method = str(test_id).partition("#")
    m = re.match(r"[A-Za-z_$][\w$]*", method or "")
    return cls, (m.group(0) if m else "")


def production_key(test_cls: str) -> tuple:
    """('com.x', 'Foo') for test class 'com.x.FooTest' (nested 'FooTest$Inner'
    too); ('', '') when the name does not follow a test naming convention."""
    pkg, _, name = test_cls.rpartition(".")
    top = name.split("$", 1)[0]
    for suf in _TEST_SUFFIXES:
        if top.endswith(suf) and len(top) > len(suf):
            return pkg, top[: -len(suf)]
    return "", ""


def test_file_for(repo_root, test_cls: str) -> str | None:
    """Repo-relative path of the .java file that declares test_cls."""
    pkg, _, name = test_cls.rpartition(".")
    top = name.split("$", 1)[0]
    rel_tail = (pkg.replace(".", "/") + "/" if pkg else "") + top + ".java"
    root = Path(repo_root)
    hits = []
    for base in ("src/test/java", "*/src/test/java", "*/*/src/test/java"):
        hits += [p for p in root.glob(f"{base}/{rel_tail}") if p.is_file()]
    if not hits:
        return None
    return sorted(hits)[0].relative_to(root).as_posix()


# ----------------------------------------------------------- java methods
def _code_mask(src: str) -> list:
    """True for each char that is code — not inside a comment, string, char
    literal or text block."""
    n = len(src)
    mask = [True] * n
    i = 0
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                mask[k] = False
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                mask[k] = False
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            for k in range(i, j):
                mask[k] = False
            i = j
        elif c in ('"', "'"):
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(n, j + 1)
            for k in range(i, j):
                mask[k] = False
            i = j
        else:
            i += 1
    return mask


def _match_close(src: str, mask: list, i: int, open_c: str, close_c: str) -> int:
    """Index of the bracket closing the one at i, or -1."""
    depth = 0
    for k in range(i, len(src)):
        if not mask[k]:
            continue
        if src[k] == open_c:
            depth += 1
        elif src[k] == close_c:
            depth -= 1
            if depth == 0:
                return k
    return -1


def method_span(src: str, name: str) -> tuple | None:
    """(start, end) of the declaration of method `name`: its annotation lines,
    signature and body. None when not declared. The first declaration wins
    (test methods are not overloaded in practice)."""
    if not name:
        return None
    mask = _code_mask(src)
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", src):
        s = m.start()
        if not mask[s]:
            continue
        # a call is preceded by '.', '(', '=', ',', '!' ...; a declaration by
        # its return type (an identifier, '>' or ']')
        k = s - 1
        while k >= 0 and src[k] in " \t\r\n":
            k -= 1
        if k < 0 or not (src[k].isalnum() or src[k] in "_$>]"):
            continue
        word = re.search(r"([\w$]+)\s*$", src[:k + 1])
        if word and word.group(1) in ("new", "return", "throw", "else", "case"):
            continue
        p_close = _match_close(src, mask, m.end() - 1, "(", ")")
        if p_close < 0:
            continue
        rest = re.match(r"\s*(?:throws\s+[\w$.,\s<>]+?)?\s*\{", src[p_close + 1:])
        if not rest:
            continue
        b_open = p_close + 1 + rest.end() - 1
        b_close = _match_close(src, mask, b_open, "{", "}")
        if b_close < 0:
            continue
        start = src.rfind("\n", 0, s) + 1
        # annotation lines directly above (@Test, @Disabled, @ParameterizedTest ...)
        while start > 0:
            prev_start = src.rfind("\n", 0, start - 1) + 1
            line = src[prev_start:start - 1].strip()
            if line.startswith("@"):
                start = prev_start
            else:
                break
        return start, b_close + 1
    return None


def method_text(src: str, name: str) -> str | None:
    sp = method_span(src, name)
    return src[sp[0]:sp[1]] if sp else None


def _norm(text: str | None) -> str | None:
    return None if text is None else re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------- snapshots
def snapshot(repo_root, test_ids) -> dict:
    """{id: {"file": rel or None, "text": method text or None}} as on disk now."""
    root = Path(repo_root)
    out, cache = {}, {}
    for tid in test_ids or []:
        cls, name = split_id(tid)
        rel = test_file_for(root, cls)
        text = None
        if rel:
            if rel not in cache:
                try:
                    cache[rel] = (root / rel).read_text(encoding="utf-8")
                except Exception:
                    cache[rel] = None
            if cache[rel] is not None:
                text = method_text(cache[rel], name)
        out[tid] = {"file": rel, "text": text}
    return out


def changes(repo_root, before: dict) -> list:
    """[(id, 'changed' | 'removed')] for methods that existed in `before` and
    are different or gone now. Whitespace-only edits do not count."""
    now = snapshot(repo_root, list(before))
    out = []
    for tid, b in before.items():
        if b.get("text") is None:
            continue
        n = now.get(tid, {}).get("text")
        if n is None:
            out.append((tid, "removed"))
        elif _norm(n) != _norm(b["text"]):
            out.append((tid, "changed"))
    return out


def restore(repo_root, before: dict, test_ids) -> tuple:
    """Put each method back as it was in `before`. Returns (restored, failed)."""
    root = Path(repo_root)
    restored, failed = [], []
    for tid in test_ids:
        b = before.get(tid) or {}
        rel, old = b.get("file"), b.get("text")
        _cls, name = split_id(tid)
        try:
            p = root / rel
            src = p.read_text(encoding="utf-8")
            sp = method_span(src, name)
            if not sp or old is None:
                failed.append(tid)
                continue
            p.write_text(src[:sp[0]] + old + src[sp[1]:], encoding="utf-8")
            restored.append(tid)
        except Exception:
            failed.append(tid)
    return restored, failed


# ----------------------------------------------------------------- triage
_TRIAGE_LINE = re.compile(
    r"^\s*[-*]\s*`?(?P<id>[\w.$]+#[^\s:`]+)`?\s*:\s*(?P<v>[A-Za-z]+)\b\s*[-—–:]*\s*(?P<r>.*)$")


def read_triage(repo_root) -> dict:
    """{id: (VERDICT, reason)} from the triage file; unknown verdicts dropped."""
    p = Path(repo_root) / TRIAGE_FILE
    out = {}
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return out
    for line in text.splitlines():
        m = _TRIAGE_LINE.match(line)
        if not m:
            continue
        v = m.group("v").upper()
        if v in VERDICTS:
            out[m.group("id")] = (v, m.group("r").strip())
    return out


# ------------------------------------------------- unit-test mode: scope
def in_scope(known: list, files: list) -> dict:
    """{id: production file} for known-failing tests whose test class names a
    production file in `files` (by package and class name)."""
    from unit_test_mode import _java_parts
    by_key = {}
    for f in files or []:
        k = _java_parts(f)
        if k[1]:
            by_key[k] = f
    out = {}
    for tid in known or []:
        cls, _m = split_id(tid)
        f = by_key.get(production_key(cls))
        if f:
            out[tid] = f
    return out


def _ut(run) -> dict:
    return getattr(run, "unit_test", None) or {}


def batch_failing(run) -> list:
    """Already-failing tests of the classes in this run's batch (triaged)."""
    ut = _ut(run)
    targets = set(ut.get("targets") or [])
    return [t for t, f in (ut.get("failing_in_scope") or {}).items() if f in targets]


def repair_allowed(run) -> bool:
    return bool(_ut(run).get("repair_failing"))


def repaired(run) -> list:
    return list(_ut(run).get("repaired") or [])


def gate_known_failing(run, known: list) -> list:
    """The already-failing list the build gate ignores: a repaired test is
    taken off it, so it must pass."""
    rep = set(repaired(run))
    return [t for t in known if t not in rep]


# ------------------------------------------------- unit-test mode: prompts
def unit_test_prompt(run, phase_id: str, known: list) -> str:
    """What unit_testing / code_review are told in unit-test mode."""
    if not known:
        return ""
    if phase_id == "code_review":
        rep = repaired(run)
        lines = ["", "", f"TESTS THAT ALREADY FAIL ON THE BASE BRANCH: {len(known)}. "
                 "The harness put back any edit to them that was not an allowed repair."]
        if rep:
            lines.append(
                f"{len(rep)} of them were REPAIRED by this run (the test author marked them "
                "STALE). The review material shows each one before and after. Judge each "
                "repair: it is a (CORRECTNESS) issue when the new expectation just copies "
                "what the code does without a believable reason that the old one is out of "
                "date, or when the behaviour now asserted looks like a bug; it is a "
                "(CONTRACT) issue when the repair removes, loosens or disables a check "
                "(fewer assertions, assertTrue(true), a wider expected value, @Disabled, "
                "an emptied body).")
        return "\n".join(lines) + "\n"
    if phase_id != "unit_testing":
        return ""
    ours = batch_failing(run)
    allow = repair_allowed(run)
    lines = ["", "",
             "TESTS THAT ALREADY FAIL ON THE BASE BRANCH (before any change by this run):"]
    lines += [f"  - {t}" for t in known[:_LIST_CAP]]
    if len(known) > _LIST_CAP:
        lines.append(f"  ... and {len(known) - _LIST_CAP} more")
    if allow:
        lines.append(
            "Do NOT edit or delete these tests, except a test listed under REPAIR below. "
            "The harness puts back any other edit and sends you back if one is deleted.")
    else:
        lines.append(
            "Do NOT edit or delete these tests. The harness puts back any edit to them and "
            "sends you back if one is deleted. Write your new tests alongside them.")
    if ours:
        lines += ["", f"TRIAGE (required): these already-failing tests belong to classes in "
                      f"this batch. Read each test and the code it covers, then write one "
                      f"line per test to {TRIAGE_FILE} (create it; the .harness folder "
                      f"exists):",
                  "    - <class>#<method>: STALE | DEFECT | FLAKY — <one-line reason>",
                  "  STALE  = the code's behaviour is clearly intended and the test's "
                  "expectation is out of date (e.g. a renamed field, a changed format "
                  "that the code applies consistently).",
                  "  DEFECT = the test's expectation looks right and the code looks wrong. "
                  f"Also add a line to {'.harness/suspected-defects.md'}.",
                  "  FLAKY  = the test depends on time, order, environment or real I/O.",
                  "  If unsure between STALE and DEFECT, choose DEFECT: a person decides."]
        lines += [f"  - {t}" for t in ours[:_LIST_CAP]]
        if allow:
            lines += ["",
                      "REPAIR (enabled for this repo): you MAY fix a test from the triage "
                      "list that you marked STALE, so that it passes against the current "
                      "code. Rules:",
                      "  - edit only that test method; keep its name; never delete it;",
                      "  - correct the expectation, do not weaken it: no fewer assertions, "
                      "no assertTrue(true), no wider expected values, no @Disabled, no "
                      "emptied body;",
                      "  - never repair a test you marked DEFECT or FLAKY;",
                      "  - a repaired test is no longer ignored: the harness runs it and it "
                      "must pass. The reviewer sees it before and after."]
        else:
            lines.append("Triage only: do not edit these tests. Repair is off for this repo.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------- the guard (per phase)
def guard_phases(run) -> tuple:
    from unit_test_mode import is_unit_test
    return ("unit_testing",) if is_unit_test(run) else ("coding", "unit_testing")


def before_phase(run, repo_root, phase_id: str, known: list) -> dict | None:
    """Snapshot the already-failing test methods before a phase that may edit
    tests. None when there is nothing to guard."""
    if not known or phase_id not in guard_phases(run):
        return None
    snap = snapshot(repo_root, known)
    # A test deleted by an earlier pass and not restored since is still
    # missing: compare with the last text seen, so the deletion stays caught.
    seen = (getattr(run, "unit_test", None) or {}).get("failing_seen") or {}
    for tid, v in snap.items():
        if v.get("text") is None and (seen.get(tid) or {}).get("text"):
            snap[tid] = dict(seen[tid])
    return snap


def after_phase(run, repo_root, phase_id: str, before: dict | None, log=print) -> str:
    """Apply the rules after the phase. Returns '' to carry on, or feedback
    text to send unit_testing back with (a deleted test, unit-test mode)."""
    from unit_test_mode import is_unit_test
    if not before:
        return ""
    found = changes(repo_root, before)
    if not is_unit_test(run):
        if found:
            b = run.baseline
            edited = list(dict.fromkeys(list(b.get("edited") or []) + [t for t, _ in found]))
            b["edited"] = edited
            log(f"  [failing-tests] '{phase_id}' changed {len(found)} test(s) that already "
                f"failed on the base branch — kept (the story may need it) and marked in "
                f"the PR: " + ", ".join(t for t, _ in found[:5]))
        return ""

    ut = run.unit_test
    seen = dict(ut.get("failing_seen") or {})
    for tid, v in before.items():
        if v.get("text") is not None:
            seen[tid] = {"file": v.get("file"), "text": v["text"][:_TEXT_CAP]}
    ut["failing_seen"] = seen
    triage = read_triage(repo_root)
    allowed_ids = set(batch_failing(run)) if repair_allowed(run) else set()
    bad_edit, removed, ok = [], [], []
    for tid, kind in found:
        if kind == "removed":
            removed.append(tid)
        elif tid in allowed_ids and (triage.get(tid) or ("",))[0] == "STALE":
            ok.append(tid)
        else:
            bad_edit.append(tid)
    if bad_edit:
        restored, failed = restore(repo_root, before, bad_edit)
        ut["restored"] = list(dict.fromkeys(list(ut.get("restored") or []) + restored))
        log(f"  [failing-tests] put back {len(restored)} edit(s) to already-failing "
            f"test(s) that were not allowed repairs: " + ", ".join(restored[:5]))
        removed += failed     # could not splice it back: treat like a deletion
    # Which tests count as repaired: allowed, STALE, and different from the
    # base text now (an earlier repair undone again drops off the list).
    original = ut.get("failing_original") or {}
    now = snapshot(repo_root, list(original)) if original else {}
    rep = []
    for tid in sorted(allowed_ids):
        o, n = (original.get(tid) or {}).get("text"), (now.get(tid) or {}).get("text")
        if (o is not None and n is not None and _norm(o) != _norm(n)
                and (triage.get(tid) or ("",))[0] == "STALE"):
            rep.append(tid)
    ut["repaired"] = rep
    if ok:
        log("  [failing-tests] repaired (marked STALE): " + ", ".join(ok[:5])
            + " — these must now pass at the build gate")
    if removed:
        return ("You DELETED (or changed beyond recognition) test(s) that already fail on "
                "the base branch. Deleting a test is never allowed in this run. Put each "
                "one back exactly as it was, then continue:\n"
                + "".join(f"  - {t} (in {(before.get(t) or {}).get('file')})\n"
                          for t in removed)
                + "Original text of each:\n"
                + "".join(f"--- {t} ---\n{(before.get(t) or {}).get('text') or ''}\n"
                          for t in removed))
    return ""


# ------------------------------------------------- unit-test mode: prepare
def prepare(run, repo_root, files: list, known: list, settings, log=print) -> list:
    """Called while picking the batch. Records the in-scope failing tests and,
    with repair on, returns their production files to put first in the batch."""
    ut = run.unit_test
    ut["repair_failing"] = bool(getattr(settings, "repair_failing", False))
    scope = in_scope(known, files)
    ut["failing_in_scope"] = scope
    if not scope:
        return []
    log(f"  [failing-tests] {len(scope)} already-failing test(s) belong to classes in "
        f"scope" + (" — repair is ON: their classes go first in the batch"
                    if ut["repair_failing"] else " — triage only (repair is off)"))
    snap = snapshot(repo_root, list(scope))
    ut["failing_original"] = {t: {"file": v["file"],
                                  "text": (v["text"] or "")[:_TEXT_CAP] or None}
                              for t, v in snap.items()}
    if not ut["repair_failing"]:
        return []
    return list(dict.fromkeys(scope.values()))


# ------------------------------------------------- unit-test mode: review
def review_material(repo_root, run) -> str:
    """Before/after of each repaired test, and the triage, for the reviewer."""
    parts = []
    triage = read_triage(repo_root)
    rep = repaired(run)
    if rep:
        original = _ut(run).get("failing_original") or {}
        now = snapshot(repo_root, rep)
        for tid in rep:
            v, why = triage.get(tid, ("?", ""))
            parts.append(
                f"--- REPAIRED ALREADY-FAILING TEST: {tid} (triage: {v} — {why}) ---\n"
                f"BEFORE (base branch, failing):\n{(original.get(tid) or {}).get('text') or ''}\n"
                f"AFTER (this run):\n{(now.get(tid) or {}).get('text') or ''}")
    if triage:
        parts.append("--- TRIAGE OF ALREADY-FAILING TESTS (test author) ---\n"
                     + "\n".join(f"- {t}: {v} — {r}" for t, (v, r) in triage.items()))
    return "\n\n".join(parts)


# ------------------------------------------------- unit-test mode: PR
def pr_section(repo_root, run) -> list:
    """Lines for the unit-test PR section; [] when no failing test is in scope."""
    ut = _ut(run)
    scope = ut.get("failing_in_scope") or {}
    if not scope:
        return []
    triage = read_triage(repo_root)
    rep = set(repaired(run))
    ours = set(batch_failing(run))
    still = (getattr(run, "baseline", None) or {}).get("still_failing")
    lines = ["", "### Already-failing tests in scope", "",
             "These tests failed on the base branch before this run. "
             + ("Repair of tests marked STALE was enabled; each repair was reviewed."
                if ut.get("repair_failing") else
                "Repair is off: none of them was changed."), "",
             "| Test | Triage | Result |", "|---|---|---|"]
    for tid in list(scope)[:_LIST_CAP]:
        v, why = triage.get(tid, ("", ""))
        tri = f"{v} — {why}" if v else ("not triaged" if tid in ours else "not in this batch")
        if tid in rep:
            res = ("repaired — passes" if isinstance(still, list) and tid not in still
                   else "repaired")
        elif isinstance(still, list) and tid not in still:
            res = "passes now (not edited)"
        else:
            res = "still failing — left as is"
        lines.append(f"| `{tid}` | {tri.replace('|', '/')} | {res} |")
    if len(scope) > _LIST_CAP:
        lines.append(f"| ... and {len(scope) - _LIST_CAP} more | | |")
    restored = ut.get("restored") or []
    if restored:
        lines += ["", f"The harness put back {len(restored)} edit(s) to already-failing "
                      "tests that were not allowed: " + ", ".join(f"`{t}`" for t in restored[:10])]
    return lines


def metrics_fields(run) -> dict:
    ut = _ut(run)
    out = {}
    if ut:
        out.update({
            "unit_test_failing_in_scope": len(ut.get("failing_in_scope") or {}),
            "unit_test_repair_failing": bool(ut.get("repair_failing")),
            "unit_test_failing_repaired": len(ut.get("repaired") or []),
            "unit_test_failing_restored": len(ut.get("restored") or []),
        })
    b = getattr(run, "baseline", None) or {}
    if b.get("status"):
        out["baseline_failing_edited"] = len(b.get("edited") or [])
    return out
