"""
IRA Native Document Generation — Feature #4.

Create professional PDF, Word, PowerPoint, and Excel files directly from chat.

POST /document/create  — generate a document from a prompt
GET  /document/download/{doc_id} — download a generated document

Trigger phrases:
  "create a PDF of...", "make a Word document with...", "generate a PowerPoint...",
  "write an Excel spreadsheet...", "build a report on...", "create a slide deck..."

Libraries:
  PDF:  reportlab (pip install reportlab)
  DOCX: python-docx (already in requirements)
  PPTX: python-pptx (pip install python-pptx)
  XLSX: openpyxl (pip install openpyxl)
"""

from __future__ import annotations

import base64
import io
import json as _json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from api.middleware.auth import require_auth
from utils.llm import chat_complete

router = APIRouter(prefix="/document", tags=["document"])
logger = logging.getLogger("ira.document_create")

_DOC_TTL = 3600  # 1 hour — documents expire after download window

# ── Redis-backed document store (per-user, TTL-protected) ────────────────────
# Falls back to a process-local dict when Redis is unavailable (e.g., dev mode).
_DOC_FALLBACK: dict[str, dict] = {}


async def _store_doc(user_id: str, doc_id: str, file_bytes: bytes, filename: str, mimetype: str) -> None:
    payload = _json.dumps({
        "data": base64.b64encode(file_bytes).decode(),
        "filename": filename,
        "mimetype": mimetype,
        "owner": user_id,
    })
    try:
        from utils.redis_client import get_redis
        redis = get_redis()
        await redis.setex(f"document:{user_id}:{doc_id}", _DOC_TTL, payload)
    except Exception:
        _DOC_FALLBACK[f"{user_id}:{doc_id}"] = {
            "data": file_bytes, "filename": filename, "mimetype": mimetype, "owner": user_id,
        }


async def _get_doc(user_id: str, doc_id: str) -> tuple[bytes, str, str] | None:
    """Return (bytes, filename, mimetype) for this user's doc, or None if not found/expired."""
    try:
        from utils.redis_client import get_redis
        redis = get_redis()
        raw = await redis.get(f"document:{user_id}:{doc_id}")
        if not raw:
            return None
        entry = _json.loads(raw)
        if entry.get("owner") != user_id:
            return None  # Cross-user access blocked
        return base64.b64decode(entry["data"]), entry["filename"], entry["mimetype"]
    except Exception:
        fb = _DOC_FALLBACK.get(f"{user_id}:{doc_id}")
        if fb and fb.get("owner") == user_id:
            return fb["data"], fb["filename"], fb["mimetype"]
        return None

# Document type detection
_DOC_CREATE_RE = re.compile(
    r"\b(create\s+(a\s+)?(pdf|word|docx|powerpoint|pptx|excel|xlsx|spreadsheet|"
    r"slide.?deck|presentation|report|document|memo|proposal|invoice|contract)|"
    r"make\s+(a\s+)?(pdf|word|powerpoint|excel|report|document|slide)|"
    r"generate\s+(a\s+)?(pdf|word|report|presentation|document|invoice)|"
    r"write\s+(a\s+)?(report|document|memo|proposal|contract)|"
    r"build\s+(a\s+)?(report|presentation|spreadsheet|document))\b",
    re.I,
)

_FORMAT_RE = re.compile(
    r"\b(pdf|word|docx|powerpoint|pptx|excel|xlsx|spreadsheet|presentation|slides?)\b",
    re.I,
)


def is_doc_create_request(query: str) -> bool:
    return bool(_DOC_CREATE_RE.search(query))


def _detect_format(query: str) -> Literal["pdf", "docx", "pptx", "xlsx"]:
    m = _FORMAT_RE.search(query)
    if not m:
        return "pdf"
    t = m.group(1).lower()
    if t in ("excel", "xlsx", "spreadsheet"):
        return "xlsx"
    if t in ("powerpoint", "pptx", "presentation", "slides", "slide"):
        return "pptx"
    if t in ("word", "docx"):
        return "docx"
    return "pdf"


# ── Request model ─────────────────────────────────────────────────────────────

class DocCreateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    format: Optional[Literal["pdf", "docx", "pptx", "xlsx"]] = None
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


# ── LLM content generation ────────────────────────────────────────────────────

_DOC_SYSTEM = """\
You are a professional document writer. Generate structured document content based on the user's request.

Output the document content in clean Markdown with:
- Clear headings (# ## ###)
- Bullet points and numbered lists where appropriate
- Tables using Markdown table syntax where relevant
- Bold key terms
- Professional, concise language

For presentations: structure as ## Slide 1: Title / content / ## Slide 2: ...
For spreadsheets: structure as a Markdown table with headers and data rows
For reports/docs: standard professional document structure

Generate COMPLETE content — do not truncate or say "continue...".
"""


_DOC_FMT_HINTS = {
    "pdf": "Create a professional PDF report/document.",
    "docx": "Create a professional Word document.",
    "pptx": "Create a presentation with clear slide titles and content. Format each slide as '## Slide N: [Title]' followed by content.",
    "xlsx": "Create a spreadsheet. Format data as a proper Markdown table with headers. Include multiple sections if needed.",
}


async def _generate_doc_content(prompt: str, fmt: str) -> str:
    msgs = [
        {"role": "system", "content": _DOC_SYSTEM},
        {"role": "user", "content": f"{_DOC_FMT_HINTS.get(fmt, '')}\n\n{prompt}"},
    ]
    return await chat_complete(msgs, use_deep=True, max_tokens=4096, temperature=0.4)


async def _draft_doc_content(prompt: str, fmt: str) -> tuple[str, Optional[dict]]:
    """Drafting entry point. When reflexion is enabled it refines the document
    through the grounded self-correction loop (this is a non-voice, non-conversational
    drafting task, so the latency guard in should_reflect() permits it); otherwise it
    falls back to the single-shot generation — behaviour is byte-identical with the
    flag OFF. Returns (content, reflexion_meta-or-None)."""
    from agents.reflexion import should_reflect, run_reflexion

    if not should_reflect(is_voice=False, is_conversational=False):
        return await _generate_doc_content(prompt, fmt), None

    task = f"{_DOC_SYSTEM}\n\n{_DOC_FMT_HINTS.get(fmt, '')}\n\n{prompt}"
    result = await run_reflexion(task, task_kind="general", use_deep=True)
    meta = {
        "reflexion": True,
        "passed": result.passed,
        "rounds": result.rounds,
        "scores": result.scores,        # the score curve, for plotting
        "grounded_by": result.grounded_by,
    }
    return (result.final or ""), meta


# ── Reading the model's Markdown ──────────────────────────────────────────────
#
# The PDF, Word and Excel writers read the same Markdown-ish text the model
# returns, through these helpers, so a table, a bold run or a stray control
# character means the same thing in all three.

#: Characters XML 1.0 cannot hold. python-docx and openpyxl refuse the whole
#: document when one appears (a model can emit a form feed or a NUL), so they go.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: A Markdown table's delimiter row: "|---|:--:|" or "--- | ---", outer pipes
#: optional. As in GitHub's Markdown, a table is a header row followed by one of
#: these; a sentence that merely contains "|" is text.
_TABLE_DELIMITER = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_NUMBERED = re.compile(r"^\d+\. ")


def _clean(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)


def _table_cells(line: str) -> list[str]:
    """One table row's cells, empty ones kept: '| a |  | c |' -> ['a', '', 'c'].

    The spreadsheet writer used to drop empty cells, which moved every later
    value in the row one column to the left.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _blocks(content: str):
    """The content in order: ("table", header, rows) for each Markdown table, rows
    padded to the widest row, and ("line", text) for every other line, stripped."""
    lines = _clean(content).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if "|" in line and _TABLE_DELIMITER.match(nxt):
            header = _table_cells(line)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_table_cells(lines[i]))
                i += 1
            width = max([len(header)] + [len(r) for r in rows])
            yield ("table", header + [""] * (width - len(header)),
                   [r + [""] * (width - len(r)) for r in rows])
            continue
        yield ("line", line)
        i += 1


def _bold_runs(text: str) -> list[tuple[str, bool]]:
    """'a **b** c' -> [('a ', False), ('b', True), (' c', False)]."""
    return [(part, i % 2 == 1) for i, part in enumerate(_BOLD.split(text)) if part]


def _plain(text: str) -> str:
    """The text without bold markers, where it is bold anyway (headings, headers)."""
    return _BOLD.sub(r"\1", text)


# ── PDF generation ────────────────────────────────────────────────────────────

def _generate_pdf(content: str, title: str) -> bytes:
    try:
        from xml.sax.saxutils import escape

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=25*mm, leftMargin=25*mm,
                                topMargin=25*mm, bottomMargin=25*mm)
        styles = getSampleStyleSheet()
        story = []

        def inline(text: str) -> str:
            # reportlab reads a Paragraph as markup, so the model's text is escaped
            # first: "R&D" stays "R&D", and "<script>" is text, not an unclosed tag
            # that aborted the whole document.
            return "".join(f"<b>{escape(t)}</b>" if bold else escape(t)
                           for t, bold in _bold_runs(text))

        # Title
        title_style = ParagraphStyle("CustomTitle", parent=styles["Title"], fontSize=18, spaceAfter=16)
        story.append(Paragraph(escape(_clean(title)), title_style))
        story.append(Spacer(1, 8))

        for kind, *rest in _blocks(content):
            if kind == "table":
                header, rows = rest
                cell = styles["BodyText"]
                data = [[Paragraph(f"<b>{escape(_plain(c))}</b>", cell) for c in header]]
                data += [[Paragraph(inline(c), cell) for c in row] for row in rows]
                table = Table(data, colWidths=[doc.width / len(header)] * len(header), repeatRows=1)
                table.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]))
                story.append(table)
                story.append(Spacer(1, 6))
                continue
            line = rest[0]
            if not line:
                story.append(Spacer(1, 6))
            elif line.startswith("### "):
                story.append(Paragraph(escape(_plain(line[4:])), styles["Heading3"]))
            elif line.startswith("## "):
                story.append(Paragraph(escape(_plain(line[3:])), styles["Heading2"]))
            elif line.startswith("# "):
                story.append(Paragraph(escape(_plain(line[2:])), styles["Heading1"]))
            elif line.startswith(("- ", "* ", "• ")):
                story.append(Paragraph(f"• {inline(line[2:])}", styles["Normal"]))
            else:
                # A numbered item keeps its number: the PDF has no list numbering of
                # its own, so stripping "1. " left the steps unnumbered.
                story.append(Paragraph(inline(line), styles["Normal"]))

        doc.build(story)
        return buf.getvalue()
    except ImportError:
        # Fallback: plain text PDF via basic bytes
        logger.warning("reportlab not installed — generating plain text fallback")
        text = f"{title}\n{'='*len(title)}\n\n{content}"
        return text.encode("utf-8")


# ── DOCX generation ───────────────────────────────────────────────────────────

def _generate_docx(content: str, title: str) -> bytes:
    try:
        from docx import Document

        def add_runs(paragraph, text: str) -> None:
            for part, bold in _bold_runs(text):
                run = paragraph.add_run(part)
                if bold:
                    run.bold = True

        doc = Document()
        doc.add_heading(_clean(title), 0)

        for kind, *rest in _blocks(content):
            if kind == "table":
                header, rows = rest
                table = doc.add_table(rows=1 + len(rows), cols=len(header))
                table.style = "Table Grid"
                for j, text in enumerate(header):
                    table.cell(0, j).paragraphs[0].add_run(_plain(text)).bold = True
                for i, row in enumerate(rows, 1):
                    for j, text in enumerate(row):
                        add_runs(table.cell(i, j).paragraphs[0], text)
                continue
            line = rest[0]
            if not line:
                doc.add_paragraph()
            elif line.startswith("### "):
                doc.add_heading(_plain(line[4:]), level=3)
            elif line.startswith("## "):
                doc.add_heading(_plain(line[3:]), level=2)
            elif line.startswith("# "):
                doc.add_heading(_plain(line[2:]), level=1)
            elif line.startswith(("- ", "* ", "• ")):
                add_runs(doc.add_paragraph(style="List Bullet"), line[2:])
            elif _NUMBERED.match(line):
                add_runs(doc.add_paragraph(style="List Number"), _NUMBERED.sub("", line))
            else:
                add_runs(doc.add_paragraph(), line)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    except ImportError:
        logger.warning("python-docx not installed")
        return content.encode("utf-8")


# ── PPTX generation ───────────────────────────────────────────────────────────

#: Bullets per slide. Past this a slide continues on the next one, "(cont.)":
#: the generator used to stop at 8 and drop the rest without a word.
_PPTX_BULLETS_PER_SLIDE = 8

#: A line that starts a slide. The model is asked for "## Slide N: Title", and a
#: local model often writes "# Slide N: Title", "**Slide N: Title**" or plain
#: "Slide N: Title" instead; each of those used to produce an empty deck.
_SLIDE_TITLE = re.compile(
    r"^(?:#{1,6}\s+(?P<heading>.+?)"
    r"|\*\*(?P<bold>Slide\s*\d+\b.*?)\*\*:?"
    r"|(?P<plain>Slide\s*\d+\s*[:.\-–—].*))\s*$",
    re.IGNORECASE)
_SLIDE_NUMBER = re.compile(r"^Slide\s*\d+\s*[:.\-–—]?\s*", re.IGNORECASE)


def _parse_slides(content: str, deck_title: str) -> list[tuple[str, list[str]]]:
    """Markdown from the model → [(slide title, bullets)], losing nothing.

    Text before the first slide marker opens the deck under its own title,
    instead of being dropped. A slide with more bullets than fit continues on
    the next, and bold markers are removed rather than shown as asterisks.
    A slide with a title and nothing under it is kept: a section divider or a
    closing "Thank you" is content. The one exception is a bare heading that
    only repeats the deck's title, which the title slide already shows.
    """
    slides: list[tuple[str, list[str]]] = []
    current: Optional[str] = None
    bullets: list[str] = []

    def flush() -> None:
        if current is None:
            return
        if not bullets:
            if current.casefold() != deck_title.strip().casefold():
                slides.append((current, []))
            return
        for start in range(0, len(bullets), _PPTX_BULLETS_PER_SLIDE):
            heading = current if start == 0 else f"{current} (cont.)"
            slides.append((heading, bullets[start:start + _PPTX_BULLETS_PER_SLIDE]))

    for raw in content.split("\n"):
        line = raw.strip()
        if not line or set(line) <= set("-*_="):          # blank or a rule
            continue
        m = _SLIDE_TITLE.match(line)
        if m:
            flush()
            found = m.group("heading") or m.group("bold") or m.group("plain") or ""
            current = _SLIDE_NUMBER.sub("", found.replace("**", "")).strip() or deck_title
            bullets = []
            continue
        if current is None:
            current = deck_title
        if line.startswith(("- ", "* ", "• ")):
            line = line[2:]
        bullets.append(line.replace("**", "").strip())
    flush()
    return slides


def _generate_pptx(content: str, title: str) -> bytes:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor

        prs = Presentation()
        # Title slide
        slide_layout = prs.slide_layouts[0]
        slide = prs.slides.add_slide(slide_layout)
        slide.shapes.title.text = title
        if slide.placeholders[1].has_text_frame:
            slide.placeholders[1].text = "Generated by IRA"

        for stitle, bullets in _parse_slides(content, title):
            sl = prs.slides.add_slide(prs.slide_layouts[1])  # Title + Content
            sl.shapes.title.text = stitle
            tf = sl.placeholders[1].text_frame
            tf.clear()
            for i, b in enumerate(bullets):
                if i == 0:
                    tf.paragraphs[0].text = b
                else:
                    p = tf.add_paragraph()
                    p.text = b
                    p.level = 1

        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue()
    except ImportError:
        logger.warning("python-pptx not installed — pip install python-pptx")
        return content.encode("utf-8")


# ── XLSX generation ───────────────────────────────────────────────────────────

#: A plain number as a model writes one: "3", "-4.5", "1,200". Leading zeros
#: ("007") and anything longer than Excel's 15 significant digits stay text.
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?:\.\d+)?$")


def _xlsx_value(text: str):
    """A cell's value: a number when the text is a plain number, so sums work;
    otherwise the text."""
    if _NUMBER.match(text) and len(re.sub(r"\D", "", text)) <= 15:
        n = text.replace(",", "")
        return float(n) if "." in n else int(n)
    return text


def _generate_xlsx(content: str, title: str) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill

        def put(ws, row: int, col: int, text: str):
            cell = ws.cell(row=row, column=col, value=_xlsx_value(text))
            if isinstance(cell.value, str) and cell.value.startswith("="):
                # openpyxl stores any string that starts with "=" as a formula. The
                # model's text is data: "=HYPERLINK(...)" or "=WEBSERVICE(...)" must
                # be shown, not run when the file is opened.
                cell.data_type = "s"
            return cell

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = re.sub(r"[\\/?*\[\]:]", " ", _clean(title))[:31] or "Sheet1"  # Excel's rules

        put(ws, 1, 1, _clean(title)).font = Font(bold=True, size=14)
        row_num = 3

        for kind, *rest in _blocks(content):
            if kind == "table":
                header, rows = rest
                for col, text in enumerate(header, 1):
                    cell = put(ws, row_num, col, _plain(text))
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill("solid", fgColor="D3D3D3")
                row_num += 1
                for row in rows:
                    for col, text in enumerate(row, 1):
                        if text:
                            put(ws, row_num, col, _plain(text))
                    row_num += 1
                row_num += 1  # a blank row after each table
                continue
            line = rest[0]
            if not line:
                continue
            if line.startswith("|"):
                # A table row with no delimiter row above it: still cells, as before.
                for col, text in enumerate(_table_cells(line), 1):
                    if text:
                        put(ws, row_num, col, _plain(text))
                row_num += 1
            elif line.startswith("#"):
                row_num += 1
                put(ws, row_num, 1, _plain(line.lstrip("#").strip())).font = Font(bold=True, size=12)
                row_num += 1
            else:
                put(ws, row_num, 1, _plain(line))
                row_num += 1

        # Auto-size columns
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except ImportError:
        logger.warning("openpyxl not installed — pip install openpyxl")
        return content.encode("utf-8")


# ── MIME types ────────────────────────────────────────────────────────────────
_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_EXT = {"pdf": ".pdf", "docx": ".docx", "pptx": ".pptx", "xlsx": ".xlsx"}


# ── SSE create endpoint ───────────────────────────────────────────────────────

@router.post("/create")
async def document_create(
    req: DocCreateRequest,
    _user: str = Depends(require_auth),
):
    """Generate a document from a prompt and return a download link (SSE)."""
    fmt = req.format or _detect_format(req.prompt)

    async def gen():
        t0 = time.monotonic()
        doc_name = re.sub(r"[^a-z0-9_]", "_", req.prompt[:40].lower()).strip("_")
        filename = f"ira_{doc_name}{_EXT[fmt]}"

        yield {"data": _json.dumps({"token": f"📄 Generating {fmt.upper()} document…\n\n"})}

        try:
            # Step 1: Generate content with LLM (reflexion-refined when enabled)
            content, reflexion_meta = await _draft_doc_content(req.prompt, fmt)
            if reflexion_meta is not None:
                yield {"data": _json.dumps({"reflexion": reflexion_meta})}
            yield {"data": _json.dumps({"token": "✅ Content generated — building document…\n"})}

            # Step 2: Convert to file bytes
            title = doc_name.replace("_", " ").title()
            if fmt == "pdf":
                file_bytes = _generate_pdf(content, title)
            elif fmt == "docx":
                file_bytes = _generate_docx(content, title)
            elif fmt == "pptx":
                file_bytes = _generate_pptx(content, title)
            else:  # xlsx
                file_bytes = _generate_xlsx(content, title)

            # Step 3: Store with full UUID (not truncated 8-char) and per-user key
            doc_id = str(uuid.uuid4())
            await _store_doc(_user, doc_id, file_bytes, filename, _MIME[fmt])

            latency = int((time.monotonic() - t0) * 1000)
            yield {"data": _json.dumps({
                "document_created": True,
                "doc_id": doc_id,
                "filename": filename,
                "format": fmt,
                "size_kb": len(file_bytes) // 1024,
                "download_url": f"/api/v1/document/download/{doc_id}",
            })}
            yield {"data": _json.dumps({
                "token": (
                    f"\n📎 **{filename}** ready to download!\n"
                    f"Size: {len(file_bytes)//1024}KB · [{fmt.upper()}]\n\n"
                    f"[⬇️ Download]({f'/api/v1/document/download/{doc_id}'})"
                )
            })}
        except Exception as e:
            yield {"data": _json.dumps({"token": f"\n❌ Document generation error: {str(e)[:200]}"})}

        yield {"data": _json.dumps({
            "done": True, "agent": "document_create",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        })}

    return EventSourceResponse(gen())


# ── Download endpoint ─────────────────────────────────────────────────────────

@router.get("/download/{doc_id}")
async def document_download(
    doc_id: str,
    _user: str = Depends(require_auth),
):
    """Download a previously generated document by its ID (owner-only, 1 h TTL)."""
    entry = await _get_doc(_user, doc_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Document not found or expired.")
    file_bytes, filename, mimetype = entry
    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
