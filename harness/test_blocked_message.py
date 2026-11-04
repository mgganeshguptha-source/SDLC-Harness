"""A MANUAL_CHANGE_REQUIRED halt names what is missing, and its resume steps
match the workflow form (OP018-6434, 2026-11-04)."""
from pathlib import Path

from blocked import BlockedDeclaration, halt_message, read_declaration


def _write(tmp_path: Path, text: str):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "blocked.md").write_text(text, encoding="utf-8")


def test_missing_and_searched_are_read_and_shown(tmp_path: Path):
    _write(tmp_path, "**BLOCKED: MANUAL_CHANGE_REQUIRED**\n"
                     "NEEDS: SOAP request classes\n"
                     "MISSING: com.x.GetAccumRequest, com.x.GetAccumResponse\n"
                     "SEARCHED: repo source, pom.xml files\n"
                     "FILE: app/pom.xml\n"
                     "WHY: outside src/main\n"
                     "SUGGESTED: UNKNOWN\n")
    decl = read_declaration(tmp_path)
    assert decl.missing == "com.x.GetAccumRequest, com.x.GetAccumResponse"
    assert decl.searched == "repo source, pom.xml files"
    msg = halt_message("OP-1", "coding", decl)
    assert "com.x.GetAccumRequest, com.x.GetAccumResponse" in msg
    assert "repo source, pom.xml files" in msg


def test_older_declaration_still_works(tmp_path: Path):
    _write(tmp_path, "**BLOCKED: MANUAL_CHANGE_REQUIRED**\nNEEDS: x\nFILE: pom.xml\n")
    msg = halt_message("OP-1", "coding", read_declaration(tmp_path))
    assert "What is needed : x" in msg
    assert "Missing" not in msg


def test_message_does_not_ask_for_start_phase():
    msg = halt_message("OP-1", "coding", BlockedDeclaration(declared=True, needs="x"))
    assert "start_phase" not in msg
    assert "resume      = true" in msg


def test_no_halt_message_asks_for_start_phase():
    # start_phase is hidden on the caller form; a blank value resumes where the
    # run halted (run-state.json).
    for name in ("attachments.py", "blocked.py", "resume.py", "state_machine.py"):
        text = (Path(__file__).parent / name).read_text(encoding="utf-8")
        assert "start_phase=" not in text, name


def test_message_gives_a_route_when_the_change_is_not_needed():
    msg = halt_message("OP-1", "coding", BlockedDeclaration(declared=True, needs="x"))
    assert "If the change is not needed" in msg
    assert "start a fresh run" in msg
