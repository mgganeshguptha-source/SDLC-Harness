"""
review.py — the CODE REVIEW gate.

After the code_review phase, an INDEPENDENT reviewer LLM (a model different from
the one that wrote the code) inspects the change and writes a structured verdict
to .harness/review.md. The harness parses that verdict deterministically:

  - VERDICT: PASS                 -> advance to unit_testing
  - VERDICT: CHANGES_REQUESTED    -> loop back to coding with the issues as
                                     feedback (bounded by max_review_retries)

The verdict is machine-readable on purpose (same philosophy as the clarification
gate): the harness owns the decision from a canonical marker, not from prose. The
issues list becomes the coding phase's feedback verbatim.

Expected review.md shape (reviewer is instructed to emit exactly this):

    VERDICT: CHANGES_REQUESTED
    [ISSUE]: Owner.hasPet does not handle null name -> NPE risk
    [ISSUE]: equalsIgnoreCase called on the argument, not the field
    ...free-form review notes may follow...

or:

    VERDICT: PASS
    ...optional commentary...
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

_VERDICT = re.compile(r"^\s*VERDICT\s*:\s*(PASS|CHANGES_REQUESTED|FAIL|APPROVE|APPROVED)\b",
                      re.IGNORECASE | re.MULTILINE)
_ISSUE = re.compile(r"^\s*(?:[-*>\s]*)?\[ISSUE\]\s*:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)
_NOTE = re.compile(r"^\s*(?:[-*>\s]*)?\[NOTE\]\s*:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)

_PASS_TOKENS = {"pass", "approve", "approved"}

# ---- SEVERITY GATE -------------------------------------------------------
# Only these classes of finding may BLOCK a change. Anything else the reviewer
# raises is advisory and must not loop the pipeline back to coding.
#
# Why (run 31252416919): the reviewer's sole remaining objection was that
# fetchBookByAuthor() used intermediate local variables instead of fluent
# chaining "like fetchBook() does" — correct code, purely a consistency
# preference. It burned both retries and HALTED the story at the cap, ~15
# credits spent converging on cosmetics. A style opinion must never be able to
# stop a run; it belongs in the review notes for a human to weigh.
#
# The prompt asks the reviewer to tag each [ISSUE] with its class. This is the
# deterministic half: markdown asks, code enforces. An issue whose class is not
# in this set is downgraded to advisory and reported, not acted on.
_BLOCKING_CLASSES = {
    "CORRECTNESS",      # wrong behaviour, broken logic, bad edge-case handling
    "CONTRACT",         # wrong signature/endpoint/status/response shape
    "REACTIVE",         # blocking calls in a reactive chain, signal loss
    "CONCURRENCY",      # thread-safety, resource leaks
    "SECURITY",         # injection, secret exposure, missing authz
    "ERROR_HANDLING",   # errors swallowed or surfaced wrongly
}
# Tolerate the spellings a model naturally produces for the same class.
_CLASS_ALIASES = {
    "ERROR HANDLING": "ERROR_HANDLING",
    "ERRORHANDLING": "ERROR_HANDLING",
    "REACTIVE SAFETY": "REACTIVE",
    "REACTIVE/CONCURRENCY": "REACTIVE",
    "CONCURRENCY SAFETY": "CONCURRENCY",
    "THREAD SAFETY": "CONCURRENCY",
    "API CONTRACT": "CONTRACT",
}
_CLASS_PREFIX = re.compile(r"^\(\s*([A-Za-z_/ -]+?)\s*\)\s*(.*)$", re.DOTALL)

# Classes the reviewer may use to say "this is NOT a reason to block". Only a
# finding that carries one of these (or is written as a [NOTE]) is advisory.
#
# Why this list exists (OP018-6434, 2026-11-15): the reviewer wrote
# CHANGES_REQUESTED for a null inside a Flux.zip combinator, the finding did not
# carry a recognised blocking class, and the gate turned the verdict into PASS
# without a word in the log. Any finding that was not tagged with a blocking
# class used to be advisory, so a real defect written in the wrong form went
# through. Now a finding is advisory only when the reviewer SAID it is; anything
# else under a CHANGES_REQUESTED verdict blocks.
_ADVISORY_CLASSES = {
    "STYLE", "NAMING", "CONSISTENCY", "PREFERENCE", "FORMATTING", "READABILITY",
    "COSMETIC", "NIT", "NITPICK", "DOCUMENTATION", "COMMENT", "COMMENTS",
    "MAINTAINABILITY", "TEST", "TESTS", "TESTING", "COVERAGE",
}
UNCLASSIFIED = "UNCLASSIFIED"


def _classify(issue_text: str):
    """Split '(CLASS) description' into (normalized_class|None, description).

    An issue with no parenthesised class, or an unrecognised one, returns None —
    the caller decides what to do with it.
    """
    m = _CLASS_PREFIX.match(issue_text.strip())
    if not m:
        return None, issue_text.strip()
    raw = m.group(1).strip().upper().replace("-", "_")
    cls = _CLASS_ALIASES.get(raw.replace("_", " "), raw)
    cls = _CLASS_ALIASES.get(raw, cls)
    if cls in _BLOCKING_CLASSES:
        return cls, m.group(2).strip()
    return None, issue_text.strip()


def _is_advisory(issue_text: str) -> bool:
    """True only when the reviewer tagged the finding with a class that says it
    is not a reason to block, e.g. '(STYLE) ...'. No tag, or a tag this gate
    does not know, is NOT advisory."""
    m = _CLASS_PREFIX.match(issue_text.strip())
    if not m:
        return False
    raw = m.group(1).strip().upper().replace("-", "_").replace(" ", "_")
    return raw in _ADVISORY_CLASSES


@dataclass
class ReviewResult:
    passed: bool
    verdict: str | None          # normalized: "PASS" | "CHANGES_REQUESTED" | None
    issues: list = field(default_factory=list)
    scanned_file: str = ""
    parse_ok: bool = True        # False when no VERDICT marker was found
    stale: bool = False          # True when the file predates this review attempt
    # Findings the reviewer raised that are NOT in a blocking class (style,
    # naming, consistency, preference). Reported to the human, never looped on.
    advisory: list = field(default_factory=list)
    # True when the reviewer said CHANGES_REQUESTED but labelled EVERY finding
    # as non-blocking ([NOTE] lines, or [ISSUE] lines with an advisory class),
    # so the gate downgraded the verdict to PASS. Always logged by the caller.
    downgraded: bool = False
    # Findings under CHANGES_REQUESTED that carried no recognised class. They
    # block (they are also in `issues`, prefixed "(UNCLASSIFIED)").
    unclassified: list = field(default_factory=list)


def parse_review(review_file: Path, written_after: float | None = None) -> ReviewResult:
    """Parse the reviewer's verdict file. Missing file or missing VERDICT marker
    is treated as NOT passed (fail-closed): a reviewer that didn't emit a verdict
    should not silently advance the change.

    `written_after`: a POSIX timestamp captured immediately BEFORE the reviewer
    ran. If review.md was not modified after that instant, it is a LEFTOVER from
    a previous attempt and must NOT be trusted.

    Why this exists (run 29181773991): the reviewer was unable to write review.md
    (its read permission was denied, and create/edit reads the target first). It
    emitted "VERDICT: PASS" to chat instead. The harness re-parsed the STALE
    CHANGES_REQUESTED file from attempt 1, looped back on an already-fixed issue,
    and burned the retry cap. A stale verdict is worse than no verdict: it is a
    confident answer to the wrong question. Fail closed and say so.
    """
    if not review_file.exists():
        return ReviewResult(passed=False, verdict=None, issues=[],
                            scanned_file=str(review_file),
                            parse_ok=False)

    if written_after is not None:
        try:
            mtime = review_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime <= written_after:
            # Leftover from a previous attempt — the reviewer produced nothing
            # this time. Fail closed, and make the REASON explicit so the halt
            # message blames the plumbing, not the code.
            return ReviewResult(
                passed=False, verdict=None,
                issues=["Reviewer did not write a verdict this attempt: "
                        f"{review_file.name} was not modified during the review "
                        "phase (the file on disk is left over from an earlier "
                        "attempt). This is a HARNESS/permission failure, not a "
                        "code defect — do not treat the stale verdict as current."],
                scanned_file=str(review_file),
                parse_ok=False, stale=True)

    text = review_file.read_text(encoding="utf-8", errors="replace")

    m = _VERDICT.search(text)
    if not m:
        # No canonical verdict -> fail-closed, flag as unparseable.
        return ReviewResult(passed=False, verdict=None, issues=[],
                            scanned_file=str(review_file), parse_ok=False)

    tok = m.group(1).lower()
    passed = tok in _PASS_TOKENS
    verdict = "PASS" if passed else "CHANGES_REQUESTED"

    issues = [i.strip() for i in _ISSUE.findall(text) if i.strip()]

    notes = [n.strip() for n in _NOTE.findall(text) if n.strip()]

    # ---- SEVERITY FILTER ----
    # Under CHANGES_REQUESTED every [ISSUE] falls in one of three groups:
    #   blocking     - tagged with a blocking class            -> blocks
    #   advisory     - tagged with an advisory class (STYLE..) -> never blocks
    #   unclassified - no tag, or a tag this gate does not know -> BLOCKS
    #
    # A style opinion must not stop a run (run 31252416919), so a finding the
    # reviewer LABELLED as style or a [NOTE] is advisory. But the gate no longer
    # decides that for the reviewer: a finding without a recognised class under
    # a CHANGES_REQUESTED verdict is treated as a real objection (OP018-6434 —
    # see _ADVISORY_CLASSES). The loop is bounded by max_review_retries, after
    # which a person decides.
    blocking, advisory, unclassified = [], list(notes) if not passed else [], []
    if not passed:
        for raw in issues:
            cls, desc = _classify(raw)
            if cls:
                blocking.append(f"({cls}) {desc}")
            elif _is_advisory(raw):
                advisory.append(desc)
            else:
                unclassified.append(desc)
                blocking.append(f"({UNCLASSIFIED}) {desc}")

        if not blocking and advisory:
            # The reviewer asked for changes but labelled every finding as
            # non-blocking. By its own labels nothing may block — advance, and
            # carry the notes forward for the human. The caller logs this.
            return ReviewResult(
                passed=True, verdict="PASS",
                issues=[],
                advisory=advisory,
                downgraded=True,
                scanned_file=str(review_file), parse_ok=True)
        issues = blocking

    # CHANGES_REQUESTED with no [ISSUE] and no [NOTE] line: the reviewer wrote
    # its objection as prose. Do not pass it — hand the prose to the coding
    # phase as the feedback.
    if not passed and not issues:
        after = text[m.end():].strip()
        prose = " ".join(after.split())[:1200]
        issues = [f"({UNCLASSIFIED}) Reviewer requested changes but did not list "
                  "[ISSUE] items. Its text: " + (prose or "(none — see review.md)")]
        unclassified = [issues[0]]
    return ReviewResult(passed=passed, verdict=verdict, issues=issues,
                        advisory=advisory, unclassified=unclassified,
                        scanned_file=str(review_file), parse_ok=True)
