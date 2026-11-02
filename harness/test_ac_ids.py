"""AC ids with or without the hyphen are counted (PC-102, 2026-11-02:
'### AC1 — MET' was read as no verdict at all)."""
from pathlib import Path

from ac_validation import scan_validation

VALIDATION = """# Validation
**VERDICT: INCONCLUSIVE**

### AC1 — MET
x
### AC-2 — MET
y
### AC6 — UNVERIFIABLE
z
### ac7 - NOT_MET
w
"""


def test_hyphenless_ids_are_counted(tmp_path: Path):
    hd = tmp_path / ".harness"
    hd.mkdir()
    (hd / "validation.md").write_text(VALIDATION)
    ctx = tmp_path / ".github" / "story-context-files"
    ctx.mkdir(parents=True)
    (ctx / "c.md").write_text("- AC-1: a\n- AC2: b\n- AC-6: c\n- AC7: d\n- AC-8: e\n")
    r = scan_validation(tmp_path, hd)
    assert r.met == ["AC-1", "AC-2"]
    assert r.unverifiable == ["AC-6"]
    assert r.not_met == ["AC-7"]
    assert r.unvalidated == ["AC-8"]
