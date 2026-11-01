"""A service limit that cuts a session off mid-work is an SDK error, not a
finished phase (petclinic-rest-api PC-101, 2026-11-01)."""
from sdk_runner import _INTERRUPTING_ERRORS


def _interrupts(msg: str) -> bool:
    return any(k in msg.lower() for k in _INTERRUPTING_ERRORS)


def test_quota_and_rate_limit_interrupt():
    assert _interrupts("session.error: You have exceeded your monthly quota (Request ID: C80A:1)")
    assert _interrupts("session.error: Rate limit exceeded, retry later")
    assert _interrupts("session.error: 429 Too Many Requests")


def test_other_errors_and_request_ids_do_not():
    assert not _interrupts("session.error: tool failed (Request ID: C80A:11E6D5:429:6AE7)")
    assert not _interrupts("session.error: Personal Access Tokens are not supported")
