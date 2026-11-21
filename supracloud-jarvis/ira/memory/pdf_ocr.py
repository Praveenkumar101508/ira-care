"""
OCR for single PDF pages, for the Technical Library's scanned and garbled pages.

Two local command-line tools do the work: ``pdftoppm`` (Poppler) renders one
page to a PNG and ``tesseract`` reads it. Nothing leaves the machine, and no
Python package is added: both are operator-installed system tools, so an
install without them loses OCR and nothing else.

    Debian/Ubuntu:  apt install poppler-utils tesseract-ocr
    macOS:          brew install poppler tesseract

Both are run as argument lists with no shell, from absolute paths resolved
once, with a timeout, so a hostile or pathological PDF can stall one page and
not the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 — fixed local binaries, argument lists, no shell
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Render resolution. 300 dpi is what Tesseract is tuned for; lower loses small
#: print and subscripts, higher costs time for no gain on book-sized text.
DEFAULT_DPI = 300

#: Tesseract's OpenMP threads wait on one another by spinning, so on a machine
#: whose cores are already busy one page slows tenfold or more, which can pass
#: the timeout, and the page then silently keeps its rejected text. One thread
#: does not: measured 23 October 2026 on 4 vCPU, a page took 1.3 s on an idle
#: machine; beside three busy processes it took 14.6 s with Tesseract's default
#: threads and 1.3 s with one. IRA reads one page at a time, so a single thread
#: costs nothing. An operator's own OMP_THREAD_LIMIT is kept.
TESSERACT_THREADS = "1"


class OcrUnavailable(Exception):
    """A required OCR tool is not installed."""


class OcrFailed(Exception):
    """OCR ran and did not produce text for this page."""


@dataclass(frozen=True)
class OcrTools:
    pdftoppm: str
    tesseract: str


def find_tools() -> OcrTools:
    """Absolute paths of both tools, or OcrUnavailable naming what is missing."""
    found = {name: shutil.which(name) for name in ("pdftoppm", "tesseract")}
    missing = [name for name, where in found.items() if not where]
    if missing:
        raise OcrUnavailable(
            f"OCR needs {' and '.join(missing)} on PATH "
            "(Debian/Ubuntu: apt install poppler-utils tesseract-ocr; "
            "macOS: brew install poppler tesseract)")
    return OcrTools(pdftoppm=str(found["pdftoppm"]), tesseract=str(found["tesseract"]))


def ocr_page(path: Path, page: int, *, tools: OcrTools, lang: str = "eng",
             dpi: int = DEFAULT_DPI, timeout: float = 120.0) -> str:
    """Text of one 1-based ``page`` of the PDF at ``path``.

    Raises OcrFailed when either tool exits non-zero or times out; the caller
    keeps the page's original status in that case.
    """
    with tempfile.TemporaryDirectory(prefix="ira-ocr-") as tmp:
        prefix = Path(tmp) / "page"
        _run([tools.pdftoppm, "-r", str(dpi), "-f", str(page), "-l", str(page),
              "-singlefile", "-png", "--", str(path), str(prefix)], timeout)
        image = prefix.with_suffix(".png")
        if not image.exists():
            raise OcrFailed(f"pdftoppm produced no image for page {page}")
        env = dict(os.environ)
        env.setdefault("OMP_THREAD_LIMIT", TESSERACT_THREADS)
        out = _run([tools.tesseract, str(image), "stdout", "-l", lang], timeout, env)
    return out.decode("utf-8", errors="replace").strip()


def _run(args: list[str], timeout: float, env: dict[str, str] | None = None) -> bytes:
    try:
        done = subprocess.run(  # nosec B603 — absolute tool path, no shell
            args, capture_output=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as e:
        raise OcrFailed(f"{Path(args[0]).name} timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise OcrFailed(f"{Path(args[0]).name} could not start ({type(e).__name__})") from e
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise OcrFailed(f"{Path(args[0]).name} exited {done.returncode}"
                        + (f": {detail[-1]}" if detail else ""))
    return done.stdout
