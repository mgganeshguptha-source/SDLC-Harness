"""
attachment_probe.py — check that a model really RECEIVES files sent as Copilot
SDK attachments (session.send(..., attachments=[...])), before the harness
relies on them for story attachments (design screenshots, error logs,
requirement documents).

What it does, per model:
  1. IMAGE  — sends a generated 4-colour PNG as a base64 "blob" attachment and
              asks for the colour of each quadrant. Pass/fail is checked
              automatically.
  2. TEXT   — sends a small text file (a fake error log holding a random code)
              as a "file" attachment and asks for the code. Checked
              automatically.
  3. DOCX   — builds a small Word document holding a random code and sends it
              twice: (a) the raw .docx as a "file" attachment (informational —
              shows whether the model can read Word files unconverted; does NOT
              affect the exit code), and (b) converted to Markdown by the probe
              (standard library only) and sent as a "file" attachment (checked).
  4. --image PATH (optional) — sends YOUR image (e.g. a real design screenshot)
              and prints the model's description, for a human to judge.
  5. --doc PATH (optional) — converts YOUR .docx to Markdown, sends it, and
              prints the model's summary, for a human to judge.

Every tool call is DENIED, so the model cannot open the files with its own
read/view tools: a correct answer proves the attachment itself was delivered.

Cost: one short session per check per model (about 1 premium request each on
a multiplier-1 model; more on premium models such as Opus). The SDK reports
the cost; it is printed per check.

Run (laptop, Copilot CLI logged in, or a model token in COPILOT_MODEL_TOKEN):
  python attachment_probe.py --model claude-sonnet-5
  python attachment_probe.py --model claude-sonnet-5 --model gpt-5.3-codex
  python attachment_probe.py --model claude-sonnet-5 --image ./design.png
  python attachment_probe.py --model claude-sonnet-5 --doc ./requirements.docx
  python attachment_probe.py --model claude-sonnet-5 --skip-text --skip-docx

Exit code: 0 when every automatic check passed, 1 otherwise.
Do not use real member data (PHI/PII) in --image or --doc: this is a probe.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import mimetypes
import os
import secrets
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from copilot import CopilotClient
from copilot.rpc import PermissionDecisionReject

# Quadrant layout of the generated test image (top-left, top-right,
# bottom-left, bottom-right).
QUADRANTS = (("top-left", "red", (220, 30, 30)),
             ("top-right", "green", (30, 170, 60)),
             ("bottom-left", "blue", (30, 60, 220)),
             ("bottom-right", "yellow", (240, 220, 30)))
TIMEOUT_S = 180


# ---------------------------------------------------------------- credentials
def _client_kwargs() -> dict:
    """Same credential order as the engine (sdk_runner.py)."""
    token = (os.environ.get("COPILOT_MODEL_TOKEN")
             or os.environ.get("COPILOT_GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN")
             or os.environ.get("GITHUB_TOKEN"))
    kwargs = {"working_directory": tempfile.gettempdir()}
    if token:
        os.environ["COPILOT_GITHUB_TOKEN"] = token
        kwargs["github_token"] = token
    else:
        kwargs["use_logged_in_user"] = True
    return kwargs


# ---------------------------------------------------------------- test inputs
def make_quadrant_png(size: int = 256) -> bytes:
    """A size x size PNG in four solid colours — standard library only."""
    half = size // 2
    rows = []
    for y in range(size):
        top = y < half
        left_rgb = QUADRANTS[0][2] if top else QUADRANTS[2][2]
        right_rgb = QUADRANTS[1][2] if top else QUADRANTS[3][2]
        rows.append(b"\x00" + bytes(left_rgb) * half + bytes(right_rgb) * (size - half))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def make_log_file(folder: Path) -> tuple[Path, str]:
    code = "ERR-" + secrets.token_hex(3).upper()
    p = folder / "probe-error.log"
    p.write_text(
        "2026-10-28T10:15:02Z INFO  starting visit-summary consumer\n"
        "2026-10-28T10:15:03Z WARN  retrying upstream call (1/3)\n"
        f"2026-10-28T10:15:04Z ERROR upstream failed with code {code}\n"
        "2026-10-28T10:15:04Z INFO  falling back to empty state\n",
        encoding="utf-8")
    return p, code


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def make_docx(folder: Path) -> tuple[Path, str]:
    """A minimal valid .docx (heading, paragraph, table) holding a random code
    — standard library only."""
    code = "REQ-" + secrets.token_hex(3).upper()
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"

    def cell(text):
        return f"<w:tc>{para(text)}</w:tc>"

    table = ("<w:tbl>"
             f"<w:tr>{cell('Field')}{cell('Rule')}</w:tr>"
             f"<w:tr>{cell('status')}{cell('FAILED shows the outage banner')}</w:tr>"
             f"<w:tr>{cell('requirement id')}{cell(code)}</w:tr>"
             "</w:tbl>")
    body = (para("Visit summary display rules", "Heading1")
            + para("The table below lists the display rules for this story.")
            + table)
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f"<w:document {ns}><w:body>{body}<w:sectPr/></w:body></w:document>")
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/word/document.xml" ContentType="application/'
              'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
              "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>")
    p = folder / "probe-requirements.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return p, code


def docx_to_markdown(path: Path) -> str:
    """Body text of a .docx as Markdown: headings (Heading1-6 styles), paragraphs
    and tables. Standard library only — enough for the probe; the engine will use
    a full converter. Images and text boxes are not extracted."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")

    def text_of(el):
        return "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()

    lines: list[str] = []
    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            txt = text_of(el)
            if not txt:
                continue
            style = el.find(f"{_W}pPr/{_W}pStyle")
            sval = style.get(f"{_W}val", "") if style is not None else ""
            level = sval[-1] if sval.lower().startswith("heading") and sval[-1:].isdigit() else ""
            lines.append(("#" * int(level) + " " if level else "") + txt)
            lines.append("")
        elif el.tag == f"{_W}tbl":
            rows = [[text_of(c) for c in tr.findall(f"{_W}tc")]
                    for tr in el.findall(f"{_W}tr")]
            if rows:
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("|" + "---|" * len(rows[0]))
                for r in rows[1:]:
                    lines.append("| " + " | ".join(r) + " |")
                lines.append("")
    return "\n".join(lines).strip() + "\n"


def blob(data: bytes, mime: str, name: str) -> dict:
    return {"type": "blob", "data": base64.b64encode(data).decode("ascii"),
            "mimeType": mime, "displayName": name}


# ---------------------------------------------------------------- one session
async def ask(client, model: str, prompt: str, attachments: list) -> dict:
    """One session, one prompt with attachments, every tool denied."""
    denied: list[str] = []

    def deny_all(request, invocation):
        denied.append(str(getattr(request, "kind", "?")))
        return PermissionDecisionReject(
            feedback="Tools are disabled for this probe. Answer only from the "
                     "attachments in the message.")

    out = {"text": "", "premium": 0.0, "models": set(), "error": None,
           "denied": denied}
    async with await client.create_session(on_permission_request=deny_all,
                                           model=model) as session:
        done = asyncio.Event()

        def on_event(event):
            t = event.type.value
            d = event.data
            if t == "assistant.message":
                out["text"] = getattr(d, "content", "") or out["text"]
            elif t == "assistant.usage":
                c = getattr(d, "cost", None)
                if c is not None:
                    out["premium"] += float(c)
                m = getattr(d, "model", None)
                if m:
                    out["models"].add(str(m))
            elif t == "session.error":
                out["error"] = (getattr(d, "message", None) or str(d))[:500]
            if t in ("session.idle", "session.completed", "turn.completed",
                     "session.error"):
                done.set()

        session.on(on_event)
        await session.send(prompt, attachments=attachments)
        try:
            await asyncio.wait_for(done.wait(), timeout=TIMEOUT_S)
        except asyncio.TimeoutError:
            out["error"] = f"no answer within {TIMEOUT_S}s"
    return out


def report(label: str, res: dict, ok: bool | None) -> None:
    verdict = {True: "PASS", False: "FAIL", None: "REVIEW"}[ok]
    served = ",".join(sorted(res["models"])) or "?"
    print(f"\n  [{verdict}] {label}  (served by: {served}, "
          f"premium: {res['premium']:.4f})")
    if res["denied"]:
        print(f"    tool calls denied: {len(res['denied'])} "
              f"({', '.join(sorted(set(res['denied'])))}) — the model tried to "
              f"read files itself; the answer must still come from the attachment")
    if res["error"]:
        print(f"    error: {res['error']}")
    answer = (res["text"] or "(no answer)").strip()
    print("    answer: " + answer.replace("\n", "\n            ")[:1500])


# ---------------------------------------------------------------- checks
async def probe_model(client, model: str, image: Path | None, doc: Path | None,
                      skip_text: bool, skip_docx: bool, workdir: Path) -> bool:
    print(f"\n=== model: {model} ===")
    all_ok = True

    # 1. image as blob
    png = make_quadrant_png()
    res = await ask(
        client, model,
        "The attached image is divided into four equal quadrants, each a solid "
        "colour. Reply with exactly four lines in this form and nothing else:\n"
        "top-left: <colour>\ntop-right: <colour>\nbottom-left: <colour>\n"
        "bottom-right: <colour>\nUse one plain colour word each (red, green, "
        "blue, yellow, ...). If you cannot see an image, reply NO_IMAGE.",
        [blob(png, "image/png", "probe-quadrants.png")])
    low = res["text"].lower()
    ok = bool(res["text"]) and all(
        any(line.strip().startswith(pos) and colour in line
            for line in low.splitlines())
        for pos, colour, _ in QUADRANTS)
    report("image (blob, image/png)", res, ok)
    all_ok &= ok

    # 2. text file as file attachment
    if not skip_text:
        log_path, code = make_log_file(workdir)
        res = await ask(
            client, model,
            "An error log is attached. Reply with ONLY the error code that "
            "appears on the ERROR line (it starts with ERR-), nothing else. If "
            "there is no attachment, reply NO_ATTACHMENT.",
            [{"type": "file", "path": str(log_path.resolve()),
              "displayName": log_path.name}])
        ok = code in res["text"]
        report(f"text (file attachment; expected {code})", res, ok)
        all_ok &= ok

    # 3. Word document: raw (informational) and converted (checked)
    if not skip_docx:
        docx_path, code = make_docx(workdir)
        ask_code = ("A requirements document is attached. Reply with ONLY the "
                    "requirement id from its table (it starts with REQ-), nothing "
                    "else. If you cannot read the attachment, reply NO_ATTACHMENT.")
        res = await ask(client, model, ask_code,
                        [{"type": "file", "path": str(docx_path.resolve()),
                          "displayName": docx_path.name}])
        raw_ok = code in res["text"]
        report(f"docx RAW as file attachment (informational; expected {code}; "
               f"{'model read it' if raw_ok else 'model could NOT read it'})",
               res, None)

        md_path = workdir / "probe-requirements.md"
        md_path.write_text(docx_to_markdown(docx_path), encoding="utf-8")
        res = await ask(client, model, ask_code,
                        [{"type": "file", "path": str(md_path.resolve()),
                          "displayName": "probe-requirements.docx (converted to Markdown)"}])
        ok = code in res["text"]
        report(f"docx CONVERTED to Markdown (file attachment; expected {code})", res, ok)
        all_ok &= ok

    # 4. the user's own image, for a human to judge
    if image:
        mime = mimetypes.guess_type(image.name)[0] or "image/png"
        res = await ask(
            client, model,
            "The attached image is a UI design for a web application screen. "
            "Describe it for a developer who must build it: the layout from top "
            "to bottom, every visible label and heading word for word, buttons, "
            "tables or cards and their fields, and any states shown (empty, "
            "error, loading). If you cannot see an image, reply NO_IMAGE.",
            [blob(image.read_bytes(), mime, image.name)])
        report(f"your image ({image.name}, {mime}) — judge the description", res, None)

    # 5. the user's own Word document, converted, for a human to judge
    if doc:
        md_path = workdir / (doc.stem + ".md")
        md_path.write_text(docx_to_markdown(doc), encoding="utf-8")
        print(f"\n  ({doc.name} converted: {md_path.stat().st_size} bytes of Markdown)")
        res = await ask(
            client, model,
            "A requirements document is attached (converted from Word to "
            "Markdown). Summarise it for a developer: its headings, every rule or "
            "acceptance criterion it states, and any table contents. Quote exact "
            "values. If you cannot read the attachment, reply NO_ATTACHMENT.",
            [{"type": "file", "path": str(md_path.resolve()),
              "displayName": f"{doc.name} (converted to Markdown)"}])
        report(f"your document ({doc.name}) — judge the summary", res, None)

    return all_ok


async def main(args) -> int:
    image = Path(args.image).expanduser() if args.image else None
    if image and not image.is_file():
        print(f"--image not found: {image}")
        return 2
    doc = Path(args.doc).expanduser() if args.doc else None
    if doc and not doc.is_file():
        print(f"--doc not found: {doc}")
        return 2
    if doc and doc.suffix.lower() != ".docx":
        print(f"--doc must be a .docx (got {doc.suffix}). Save legacy .doc files as "
              f".docx first.")
        return 2

    ok_all = True
    with tempfile.TemporaryDirectory(prefix="harness-probe-") as tmp:
        async with CopilotClient(**_client_kwargs()) as client:
            # Show what the service says about each model before spending.
            try:
                info = {getattr(m, "id", None): m for m in await client.list_models()}
            except Exception as e:  # never block the probe on metadata
                info = {}
                print(f"(could not list models: {e!r})")
            for model in args.model:
                m = info.get(model)
                if info and m is None:
                    print(f"\n=== model: {model} ===\n  [SKIP] not available to "
                          f"this credential (run list_models.py)")
                    ok_all = False
                    continue
                if m is not None:
                    sup = getattr(getattr(getattr(m, "capabilities", None),
                                          "supports", None), "vision", None)
                    print(f"\n(model {model} reports vision={sup})")
                try:
                    ok_all &= await probe_model(client, model, image, doc,
                                                args.skip_text, args.skip_docx,
                                                Path(tmp))
                except Exception as e:
                    print(f"\n  [FAIL] {model}: {e!r}")
                    ok_all = False

    print("\nRESULT:", "all automatic checks passed" if ok_all
          else "one or more checks failed — see above")
    return 0 if ok_all else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Check that models receive Copilot SDK attachments.")
    ap.add_argument("--model", action="append", required=True,
                    help="model id to probe (repeatable)")
    ap.add_argument("--image", help="optional real image to describe (no PHI/PII)")
    ap.add_argument("--doc", help="optional real .docx to summarise (no PHI/PII)")
    ap.add_argument("--skip-text", action="store_true",
                    help="skip the text-file attachment check")
    ap.add_argument("--skip-docx", action="store_true",
                    help="skip the Word-document checks")
    sys.exit(asyncio.run(main(ap.parse_args())))
