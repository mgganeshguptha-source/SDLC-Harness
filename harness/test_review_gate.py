"""
test_review_gate.py - the code review gate must not turn CHANGES_REQUESTED
into PASS unless the reviewer itself labelled every finding as non-blocking.

OP018-6434 (2026-11-15): the reviewer wrote CHANGES_REQUESTED for a null inside
a Flux.zip combinator; the finding carried no recognised blocking class and the
gate logged PASS.

Run:  python -m pytest harness/test_review_gate.py -v
"""
from review import parse_review


def _rv(tmp_path, text):
    f = tmp_path / "review.md"
    f.write_text(text, encoding="utf-8")
    return parse_review(f)


def test_tagged_blocking_issue_blocks(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n[ISSUE]: (CORRECTNESS) null is not handled\n")
    assert not rv.passed and rv.issues == ["(CORRECTNESS) null is not handled"]
    assert rv.unclassified == []


def test_untagged_issue_blocks_instead_of_passing(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n"
                       "[ISSUE]: Flux.zip combinator returns null -> NullPointerException\n")
    assert not rv.passed and not rv.downgraded
    assert rv.issues == ["(UNCLASSIFIED) Flux.zip combinator returns null -> NullPointerException"]
    assert rv.unclassified


def test_unknown_class_blocks(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n[ISSUE]: (CRITICAL) zip returns null\n")
    assert not rv.passed
    assert rv.issues == ["(UNCLASSIFIED) (CRITICAL) zip returns null"]


def test_prose_only_objection_blocks_and_carries_the_prose(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n\nCritical finding: the Flux.zip "
                       "combinator returns null, which throws at runtime.\n")
    assert not rv.passed and not rv.downgraded
    assert len(rv.issues) == 1 and "Flux.zip combinator returns null" in rv.issues[0]


def test_style_class_is_still_downgraded(tmp_path):
    """The cosmetic-halt protection stays: a finding the reviewer labels as
    style never blocks."""
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n"
                       "[ISSUE]: (STYLE) use fluent chaining like fetchBook()\n")
    assert rv.passed and rv.downgraded and rv.verdict == "PASS"
    assert rv.advisory == ["(STYLE) use fluent chaining like fetchBook()"]


def test_notes_only_is_downgraded(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n[NOTE]: a branch that can never run\n")
    assert rv.passed and rv.downgraded and rv.advisory == ["a branch that can never run"]


def test_a_style_finding_does_not_hide_a_real_one(tmp_path):
    rv = _rv(tmp_path, "VERDICT: CHANGES_REQUESTED\n"
                       "[ISSUE]: (STYLE) rename x\n"
                       "[ISSUE]: (SECURITY) the membership check was removed\n")
    assert not rv.passed
    assert rv.issues == ["(SECURITY) the membership check was removed"]
    assert rv.advisory == ["(STYLE) rename x"]


def test_pass_is_unchanged(tmp_path):
    rv = _rv(tmp_path, "VERDICT: PASS\n[NOTE]: tidy later\n")
    assert rv.passed and not rv.downgraded and rv.issues == []
