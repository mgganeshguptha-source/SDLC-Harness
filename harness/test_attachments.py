"""
test_attachments.py — story attachments and the input guard, with NO SDK.

Run:  python -m pytest test_attachments.py -v
"""
import base64
import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import attachments as AT
import halt_gates as HG
from config import HarnessConfig
from contracts import ExitCode
from phases import PHASES
from state import RunState
from state_machine import StateMachine

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def _not_ci(monkeypatch):
    # Laptop-like by default; tests that need CI set it themselves.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("HARNESS_CONFIRM_NO_PHI", raising=False)


def _repo(tmp: Path, config: str = "") -> Path:
    (tmp / ".harness").mkdir(parents=True, exist_ok=True)
    if config:
        (tmp / ".harness" / "config.yaml").write_text(config, encoding="utf-8")
    return tmp


def _att(repo: Path, feature="F-1") -> Path:
    d = repo / "stories" / feature
    d.mkdir(parents=True, exist_ok=True)
    return d


def _docx(path: Path, text: str, with_image: bool = False):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>'
           '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Rules</w:t></w:r></w:p>'
           f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
           '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc>'
           '<w:tc><w:p><w:r><w:t>Rule</w:t></w:r></w:p></w:tc></w:tr>'
           '<w:tr><w:tc><w:p><w:r><w:t>status</w:t></w:r></w:p></w:tc>'
           '<w:tc><w:p><w:r><w:t>FAILED shows outage</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
           '</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        if with_image:
            z.writestr("word/media/image1.png", PNG_1PX)


def _no_ocr(monkeypatch):
    monkeypatch.setattr(AT, "ocr_available", lambda: (True, ""))
    monkeypatch.setattr(AT, "ocr_text", lambda data: "")


# ------------------------------------------------------------------- scanner
@pytest.mark.parametrize("text,kind,masked", [
    ("member ssn 123-45-6789 on file", "ssn", "***-**-6789"),
    ("SSN: 123456789", "ssn", "***-**-6789"),
    ("DOB: 04/12/1980", "dob", "**/**/****"),
    ("date of birth 1980-04-12", "dob", "**/**/****"),
    ("call (313) 555-0142 now", "phone", "(***) ***-0142"),
    ("call 313-555-0142", "phone", "(***) ***-0142"),
    ("mail jane.doe@bcbsm.com today", "email", "j***@***.com"),
])
def test_scan_finds_and_masks(text, kind, masked):
    f = AT.scan_text(text, "story", ["ssn", "dob", "phone", "email"], [])
    assert [(x.kind, x.masked) for x in f] == [(kind, masked)]
    # the raw value must never appear in the finding
    shown = str(f[0])
    assert masked in shown
    assert not any(v in shown for v in ("jane.doe", "123-45", "555-0142", "04/12", "1980"))


@pytest.mark.parametrize("text", [
    "2026-10-28T10:15:02Z INFO started",          # timestamp
    "order id 000-12-3456 is not an ssn",          # impossible SSN area
    "build 1234567890123 finished",                # long id
    "version 1.2.3-4567",                          # version string
    "see docs@example.com",                        # documentation domain
    "plain date 04/12/1980 with no label",         # date without a DOB label
    "trace 123456789 in log",                      # unlabelled 9 digits
])
def test_scan_ignores_non_pii(text):
    assert AT.scan_text(text, "x", ["ssn", "dob", "phone", "email"], []) == []


def test_allow_list_and_kind_selection():
    t = "support@bcbsm.com or 313-555-0100"
    assert AT.scan_text(t, "x", ["email", "phone"], ["SUPPORT@bcbsm.com", "313-555-0100"]) == []
    assert [f.kind for f in AT.scan_text(t, "x", ["email"], [])] == ["email"]


# ------------------------------------------------------------------- prepare
def test_no_folder_no_change(tmp_path):
    repo = _repo(tmp_path)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "a clean story")
    assert res.ok and res.items == []
    assert AT.load_manifest(repo) is None


def test_story_pii_halts_even_without_attachments(tmp_path):
    repo = _repo(tmp_path)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1",
                     "Test with member 123-45-6789")
    assert not res.ok and res.findings[0].source == "story"


def test_text_image_docx_staged(tmp_path, monkeypatch):
    _no_ocr(monkeypatch)
    repo = _repo(tmp_path)
    d = _att(repo)
    (d / "error.log").write_text("ERROR upstream 500\n", encoding="utf-8")
    (d / "design.png").write_bytes(PNG_1PX)
    _docx(d / "req.docx", "Show the banner", with_image=True)
    (d / ".DS_Store").write_bytes(b"x")
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert res.ok, res.problems
    kinds = sorted((i["name"], i["kind"]) for i in res.items)
    assert ("design.png", "image") in kinds and ("error.log", "text") in kinds
    assert ("req.md", "text") in kinds and ("req-image1.png", "image") in kinds
    md = Path(next(i["path"] for i in res.items if i["name"] == "req.md")).read_text()
    assert "# Rules" in md and "| status | FAILED shows outage |" in md
    m = AT.load_manifest(repo)
    assert m["ok"] and len(m["items"]) == 4
    att = AT.sdk_attachments(m)
    assert {a["type"] for a in att} == {"blob", "file"}
    blob = next(a for a in att if a["type"] == "blob")
    assert blob["mimeType"] == "image/png" and base64.b64decode(blob["data"]) == PNG_1PX
    assert "STORY ATTACHMENTS" in AT.prompt_note(m)


def test_pii_in_attachment_halts_and_stages_nothing(tmp_path, monkeypatch):
    _no_ocr(monkeypatch)
    repo = _repo(tmp_path)
    d = _att(repo)
    (d / "error.log").write_text("ok\nuser jane.doe@bcbsm.com failed\n", encoding="utf-8")
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok
    f = res.findings[0]
    assert (f.source, f.line, f.kind) == ("stories/F-1/error.log", 2, "email")
    m = AT.load_manifest(repo)
    assert m["ok"] is False and m["items"] == []
    assert not any(p.name.startswith("01-") for p in (repo / ".harness" / "attachments").iterdir())
    msg = AT.guard_message(res, "F-1", "context")
    assert "jane.doe" not in msg and "INPUT GUARD" in msg


def test_pii_in_image_via_ocr_halts(tmp_path, monkeypatch):
    monkeypatch.setattr(AT, "ocr_available", lambda: (True, ""))
    monkeypatch.setattr(AT, "ocr_text", lambda data: "Member SSN 123-45-6789")
    repo = _repo(tmp_path)
    (_att(repo) / "screen.png").write_bytes(PNG_1PX)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok and res.findings[0].source == "stories/F-1/screen.png (OCR)"


def test_ocr_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(AT, "ocr_available", lambda: (False, "no tesseract"))
    repo = _repo(tmp_path)
    (_att(repo) / "screen.png").write_bytes(PNG_1PX)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok and "OCR is on but unavailable" in res.problems[0]


def test_scan_off_allows_mocked_data(tmp_path, monkeypatch):
    monkeypatch.setattr(AT, "ocr_available", lambda: (False, "no tesseract"))
    repo = _repo(tmp_path, "attachment_pii_scan: false\n")
    d = _att(repo)
    (d / "mock.json").write_text('{"ssn": "123-45-6789"}', encoding="utf-8")
    (d / "screen.png").write_bytes(PNG_1PX)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert res.ok and len(res.items) == 2 and not res.pii_scan
    assert any("NOT scanned" in n for n in res.notes)


@pytest.mark.parametrize("name,expect", [
    ("old.doc", "save as .docx"),
    ("sheet.xlsx", ".csv"),
    ("thing.exe", "not supported"),
])
def test_refused_types(tmp_path, name, expect):
    repo = _repo(tmp_path)
    (_att(repo) / name).write_bytes(b"data")
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok and expect in res.problems[0]


def test_size_and_count_limits(tmp_path, monkeypatch):
    _no_ocr(monkeypatch)
    repo = _repo(tmp_path, "attachment_max_file_mb: 0.0001\nattachment_max_images: 1\n")
    d = _att(repo)
    (d / "big.txt").write_text("x" * 500, encoding="utf-8")
    (d / "a.png").write_bytes(PNG_1PX)
    (d / "b.png").write_bytes(PNG_1PX)
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    text = " ".join(res.problems)
    assert not res.ok and "per-file limit" in text and "over the limit of 1" in text


def test_ci_requires_confirmation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cfg = HarnessConfig.load(repo / ".harness")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    res = AT.prepare(repo, cfg, "F-1", "story")
    assert not res.ok and "confirm_no_phi" in res.problems[0]
    monkeypatch.setenv("HARNESS_CONFIRM_NO_PHI", "false")
    assert not AT.prepare(repo, cfg, "F-1", "story").ok
    monkeypatch.setenv("HARNESS_CONFIRM_NO_PHI", "true")
    res = AT.prepare(repo, cfg, "F-1", "story")
    assert res.ok and res.confirmed is True


def test_scanned_pdf_refused(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (_att(repo) / "scan.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(AT, "pdf_to_text", lambda p: (None, "looks like a scanned PDF"))
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok and "scanned PDF" in res.problems[0]


def test_needs_ocr(tmp_path):
    repo = _repo(tmp_path)
    cfg = HarnessConfig.load(repo / ".harness")
    assert AT.needs_ocr(repo, cfg, "F-1") is False
    (_att(repo) / "a.png").write_bytes(PNG_1PX)
    assert AT.needs_ocr(repo, cfg, "F-1") is True


# ------------------------------------------------------------------- vision
def _model(vision=True, max_images=5, max_size=1_000_000, types=("image/png", "image/jpeg")):
    return SimpleNamespace(capabilities=SimpleNamespace(
        supports=SimpleNamespace(vision=vision),
        limits=SimpleNamespace(vision=SimpleNamespace(
            max_prompt_images=max_images, max_prompt_image_size=max_size,
            supported_media_types=list(types)))))


def test_check_vision():
    m = {"items": [{"kind": "image", "name": "a.png", "mime": "image/png", "bytes": 100}]}
    assert AT.check_vision(_model(), m) == ""
    assert "does not accept images" in AT.check_vision(_model(vision=False), m)
    two = {"items": m["items"] * 2}
    assert "at most 1 images" in AT.check_vision(_model(max_images=1), two)
    assert "up to 10 bytes" in AT.check_vision(_model(max_size=10), m)
    assert "image/png" in AT.check_vision(_model(types=("image/jpeg",)), m)
    assert AT.check_vision(_model(vision=False), {"items": [{"kind": "text"}]}) == ""


# ------------------------------------------------------------------- machine
class _CountingExecutor:
    def __init__(self):
        self.calls = []

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        return ExitCode.OK


def test_machine_halts_before_any_model_call(tmp_path, monkeypatch):
    _no_ocr(monkeypatch)
    repo = _repo(tmp_path)
    (_att(repo) / "log.txt").write_text("SSN 123-45-6789\n", encoding="utf-8")
    ex = _CountingExecutor()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = RunState(feature_id="F-1", story="story", current_phase=PHASES[0].id)
    # repo_instructions skips when the file exists; make it exist so the first
    # MODEL phase reached is context.
    (repo / ".github").mkdir()
    (repo / ".github" / "copilot-instructions.md").write_text("x", encoding="utf-8")
    run = sm.run_until_pause(run)
    assert run.status == "halted" and run.halt_gate == HG.INPUT_GUARD
    assert ex.calls == []
    assert run.input_guard["findings"] == 1 and run.input_guard["finding_types"] == ["ssn"]
    assert "6789" in run.halt_detail and "123-45" not in run.halt_detail
    saved = json.loads((repo / ".harness" / "run-state.json").read_text())
    assert "123-45-6789" not in json.dumps(saved)


def test_machine_records_attachments_when_clean(tmp_path, monkeypatch):
    _no_ocr(monkeypatch)
    repo = _repo(tmp_path)
    (_att(repo) / "design.png").write_bytes(PNG_1PX)
    ex = _CountingExecutor()
    sm = StateMachine(ex, repo / ".harness", repo_root=repo, log=lambda *a: None)
    run = RunState(feature_id="F-1", story="story", current_phase=PHASES[0].id)
    run = sm.step(run)  # repo_instructions: guard runs, then the executor
    assert ex.calls == [PHASES[0].id]
    assert run.attachments and run.attachments[0]["name"] == "design.png"
    assert "path" not in run.attachments[0]


# ------------------------------------------------------------------- real tools
# Run only where Tesseract / pypdf are installed (the engine workflow installs
# them); skipped otherwise so the suite stays SDK- and tool-free.
def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 50 750 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


@pytest.mark.skipif(not AT.ocr_available()[0], reason="Tesseract not installed")
def test_real_ocr_finds_ssn_in_image(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    ImageDraw.Draw(im).text((20, 50), "Member SSN 123-45-6789", fill="black", font=font)
    repo = _repo(tmp_path)
    im.save(_att(repo) / "screen.png")
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert not res.ok and any(f.kind == "ssn" for f in res.findings), res.problems


def test_real_pdf_text(tmp_path):
    pytest.importorskip("pypdf")
    repo = _repo(tmp_path)
    (_att(repo) / "spec.pdf").write_bytes(
        _minimal_pdf("The visit summary banner shows OUTAGE when status is FAILED today"))
    res = AT.prepare(repo, HarnessConfig.load(repo / ".harness"), "F-1", "story")
    assert res.ok, res.problems
    md = Path(res.items[0]["path"]).read_text()
    assert "OUTAGE when status is FAILED" in md and res.items[0]["converted_from"] == "pdf"
