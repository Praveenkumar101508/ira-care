"""A scanned PDF uploaded to chat is read, not handed to the model as nothing.

`_extract_document_text` returned only the PDF's text layer. A scanned page has
none, so a scanned document reached the model as an empty "Document contents"
block and the model answered as if the file said nothing. Pages with no text
layer are now read by OCR, with the local tools the Technical Library already
uses, within a page cap and a time budget; and whatever is left unread is said.

Only `test_a_scanned_page_is_read_by_ocr` runs the real tools, and it skips
without them. The other tests replace `ocr_page` with a fake, and those that
expect OCR to run take the `ocr_tools_present` fixture as well: the route asks
`find_tools()` before it calls `ocr_page`, so on a machine without tesseract
they used to stop at "OCR is not installed" and fail, testing nothing.
"""
import os

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

import asyncio
import io
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    setattr(_st, "SentenceTransformer", object)

import api.routes.chat as chatmod
from api.routes.chat import _extract_document_text
from memory.pdf_ocr import OcrFailed, OcrTools, OcrUnavailable
from tests.fixtures.technical_library.corpus import SCANNED_PAGE, write_pdf, write_scanned_pdf

_OCR_TOOLS = bool(shutil.which("pdftoppm") and shutil.which("tesseract"))


@pytest.fixture
def ocr_tools_present(monkeypatch):
    """The route finds both tools, whether or not this machine has them.

    For tests that fake `ocr_page`: nothing is executed, so the paths are never
    run, only passed along.
    """
    monkeypatch.setattr("memory.pdf_ocr.find_tools",
                        lambda: OcrTools(pdftoppm="pdftoppm", tesseract="tesseract"))


def _pdf(tmp_path: Path, pages: list[str]) -> bytes:
    path = tmp_path / "doc.pdf"
    write_pdf(path, pages)
    return path.read_bytes()


def _extract(content: bytes) -> str:
    return _extract_document_text(content, "doc.pdf", "application/pdf")


@pytest.mark.skipif(not _OCR_TOOLS, reason="needs pdftoppm and tesseract (CI's test job has both)")
def test_a_scanned_page_is_read_by_ocr(tmp_path, monkeypatch):
    # This tests reading, not speed: on a loaded machine one page can outlast
    # the product's limits, which the budget tests below cover.
    monkeypatch.setattr(chatmod, "_OCR_PAGE_TIMEOUT_S", 300.0)
    monkeypatch.setattr(chatmod, "_OCR_UPLOAD_BUDGET_S", 600.0)
    path = tmp_path / "scan.pdf"
    write_scanned_pdf(path, [(SCANNED_PAGE, None)])
    out = _extract(path.read_bytes())
    assert "[Page 1, read by OCR]" in out
    assert "bloom filter" in out.lower()
    assert "false positive" in out.lower()


def test_pages_with_text_are_not_ocrd(tmp_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("OCR ran on a page that has a text layer")

    monkeypatch.setattr("memory.pdf_ocr.ocr_page", never)
    out = _extract(_pdf(tmp_path, ["A text page about tariffs and duties."]))
    assert "tariffs" in out
    assert "read by OCR" not in out


def test_only_the_scanned_page_of_a_mixed_document_is_ocrd(tmp_path, monkeypatch, ocr_tools_present):
    seen = []

    def fake_ocr(path, page, **kw):
        seen.append(page)
        return "INVOICE 4471 PAID IN FULL"

    monkeypatch.setattr("memory.pdf_ocr.ocr_page", fake_ocr)
    out = _extract(_pdf(tmp_path, ["A text page about tariffs.", ""]))
    assert seen == [2]
    assert "tariffs" in out
    assert "[Page 2, read by OCR]\nINVOICE 4471 PAID IN FULL" in out


def test_without_ocr_tools_the_text_says_the_pages_were_not_read(tmp_path, monkeypatch):
    def missing():
        raise OcrUnavailable("OCR needs tesseract on PATH")

    monkeypatch.setattr("memory.pdf_ocr.find_tools", missing)
    out = _extract(_pdf(tmp_path, ["", ""]))
    assert out.strip(), "a scanned document must not reach the model as nothing"
    assert "2 page(s) have no text layer" in out
    assert "OCR is not installed" in out


def test_ocr_stops_at_the_page_cap_and_says_how_many_were_left(tmp_path, monkeypatch, ocr_tools_present):
    seen = []
    monkeypatch.setattr("memory.pdf_ocr.ocr_page",
                        lambda path, page, **kw: seen.append(page) or f"page {page} text")
    out = _extract(_pdf(tmp_path, [""] * (chatmod._OCR_MAX_PAGES + 2)))
    assert len(seen) == chatmod._OCR_MAX_PAGES
    assert "2 scanned page(s) not read" in out


def test_ocr_stops_when_the_upload_budget_is_spent(tmp_path, monkeypatch, ocr_tools_present):
    seen = []
    monkeypatch.setattr(chatmod, "_OCR_UPLOAD_BUDGET_S", 0.0)
    monkeypatch.setattr("memory.pdf_ocr.ocr_page",
                        lambda path, page, **kw: seen.append(page) or "text")
    out = _extract(_pdf(tmp_path, ["", "", ""]))
    assert seen == []
    assert "3 scanned page(s) not read" in out


def test_a_page_ocr_cannot_read_is_reported(tmp_path, monkeypatch, ocr_tools_present):
    def fails(path, page, **kw):
        raise OcrFailed("tesseract timed out after 30s")

    monkeypatch.setattr("memory.pdf_ocr.ocr_page", fails)
    out = _extract(_pdf(tmp_path, [""]))
    assert "1 scanned page(s) could not be read by OCR" in out


def test_the_upload_route_gives_the_model_the_ocr_text(tmp_path, monkeypatch, ocr_tools_present):
    from starlette.datastructures import Headers, UploadFile

    from api.routes.chat import chat_document_upload

    monkeypatch.setattr(chatmod, "ensure_conversation", AsyncMock(return_value="conv1"))
    monkeypatch.setattr("memory.store.save_message", AsyncMock())
    monkeypatch.setattr("memory.pdf_ocr.ocr_page", lambda path, page, **kw: "INVOICE 4471 PAID")

    captured = {}

    async def _fake_stream(messages, **kwargs):
        captured["messages"] = messages
        yield "ok"

    monkeypatch.setattr(chatmod, "stream_tokens", _fake_stream)

    upload = UploadFile(file=io.BytesIO(_pdf(tmp_path, [""])), filename="scan.pdf",
                        headers=Headers({"content-type": "application/pdf"}))
    async def _run():
        resp = await chat_document_upload(message="Is it paid?", session_id="s1", file=upload,
                                          _user="owner")
        async for _ in resp.body_iterator:
            pass

    asyncio.run(_run())
    doc_system = captured["messages"][1]["content"]
    assert "INVOICE 4471 PAID" in doc_system
