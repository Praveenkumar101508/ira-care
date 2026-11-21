"""A small, synthetic study library for the Technical Library tests and eval.

Every word here was written for this fixture. Nothing is taken from any real
book, course or subscriber content, and no real file name is used.

``build_corpus(root)`` writes the library to disk at test time. Binary formats
(PDF, DOCX, ZIP) are generated rather than committed, and the one file that
must exceed 5 MiB is padded on the fly, so the repository carries only this
source file. The library deliberately contains every case ingestion has to
survive:

* a byte-identical duplicate (``copies/…``)
* a PDF page with almost no extractable text (a figure page)
* a PDF page whose text has lost every letter "a" (a font-encoding defect)
* a password-protected PDF, a ZIP archive, and macOS metadata files
* a PDF larger than 5 MiB whose content must still be indexed
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — the fixture renders pages with the local pdftoppm
import tempfile
import zipfile
import zlib
from pathlib import Path

# ── Content (all invented for this fixture) ──────────────────────────────────

SQL_PAGE_1 = (
    "GROUP BY collapses rows that share the same values in the grouping columns "
    "into one summary row per group. Every column in the select list must either "
    "appear in the GROUP BY clause or be wrapped in an aggregate function such as "
    "COUNT, SUM or AVG. The HAVING clause filters groups after aggregation, while "
    "the WHERE clause filters individual rows before any grouping takes place. A "
    "common mistake is to put an aggregate condition in WHERE, which the database "
    "rejects because the aggregate does not exist yet at that stage of evaluation."
)
SQL_PAGE_2 = (
    "Window functions compute a value for every row while still looking at a set "
    "of related rows, so unlike GROUP BY they do not collapse the result. "
    "ROW_NUMBER assigns a unique sequential integer within each partition, RANK "
    "leaves gaps after ties, and DENSE_RANK assigns consecutive ranks with no "
    "gaps. The PARTITION BY clause defines the groups and ORDER BY inside the OVER "
    "clause defines the order in which rows are numbered. A running total is "
    "written as SUM(amount) OVER (ORDER BY order_date)."
)
SQL_PAGE_3_FIGURE = "Figure 3"   # a figure page: far below the OCR threshold

STATISTICS_MD = """# Hypothesis testing notes

## p-values

A p-value is the probability of observing data at least as extreme as the data
actually observed, assuming the null hypothesis is true. It is not the
probability that the null hypothesis is true. A small p-value says the observed
data would be surprising under the null hypothesis.

## Confidence intervals

A 95 percent confidence interval is produced by a procedure that, over many
repeated samples, captures the true parameter 95 percent of the time. Any one
computed interval either contains the parameter or it does not.

## Type I and Type II errors

A Type I error rejects a true null hypothesis; its rate is the significance
level alpha. A Type II error fails to reject a false null hypothesis; one minus
its rate is the statistical power of the test.
"""

RAG_EVAL_TXT = """Evaluating retrieval for a RAG system

Hit rate at k is the share of questions for which at least one relevant chunk
appears among the top k retrieved chunks. Mean reciprocal rank averages one
divided by the rank of the first relevant chunk, and scores zero for a question
where nothing relevant was retrieved.

Reciprocal Rank Fusion merges several ranked lists by summing one divided by
the constant k plus the rank of each item in each list. A constant of sixty is
the usual default. Fusion needs only ranks, so it can combine a keyword
retriever and a vector retriever whose scores are not comparable.

A golden set should include unanswerable questions, so that the evaluation also
measures whether the system correctly refuses when the corpus has no answer.
"""

LORA_DOCX_PARAGRAPHS = [
    "Low-rank adaptation for fine-tuning",
    "LoRA freezes the pretrained weight matrix and learns a low-rank update "
    "expressed as the product of two small matrices, B times A. Only A and B are "
    "trained, so the number of trainable parameters drops sharply.",
    "The rank r controls the size of the update. Typical values of r lie between "
    "4 and 64, and the scaling factor alpha divided by r multiplies the update "
    "before it is added to the frozen weights.",
    "QLoRA applies the same idea to a base model quantised to 4-bit NormalFloat, "
    "which lets a large model be fine-tuned on a single consumer GPU.",
]

PANDAS_CELLS = [
    ("markdown", "# Grouping in pandas\n\nThe groupby method splits a DataFrame "
                 "into groups, applies a function to each group, and combines the "
                 "results. This pattern is called split-apply-combine."),
    ("code", "summary = df.groupby('region')['revenue'].agg(['sum', 'mean'])\nprint(summary)"),
    ("markdown", "Use transform instead of agg when the result must keep the "
                 "original row count, for example to subtract each group's mean "
                 "from every row in that group."),
]

HNSW_PY = '''"""Notes on HNSW vector indexes.

HNSW builds a layered proximity graph. The parameter m sets how many neighbours
each node keeps, ef_construction sets the candidate list size while building,
and ef_search sets the candidate list size at query time. Raising ef_search
improves recall at the cost of latency.
"""


def hnsw_settings(m: int = 16, ef_construction: int = 64) -> dict:
    """Return index build settings with sensible defaults."""
    return {"m": m, "ef_construction": ef_construction}
'''

ATTENTION_TEX = r"""\section{Scaled dot-product attention}
Attention computes a weighted sum of value vectors. The weights come from a
softmax over the dot products of a query with every key, divided by the square
root of the key dimension. Dividing by that square root keeps the dot products
from growing so large that the softmax saturates and its gradients vanish.
\subsection{Multi-head attention}
Multi-head attention runs several attention functions in parallel on different
learned projections and concatenates their outputs.
"""

PROMPTING_PAGE_1 = (
    "Few-shot prompting places a handful of worked examples in the prompt before "
    "the real question. The examples show the model the expected format and the "
    "level of detail, which often matters more than the wording of the "
    "instruction itself. Chain of thought prompting asks the model to reason step "
    "by step before it gives a final answer, which helps on arithmetic and "
    "multi-step logic questions."
)
# The defect: every "a" silently dropped, as a broken font encoding produces.
PROMPTING_PAGE_2_GARBLED = (
    "Common mistakes when writing prompts for assistants include vague "
    "instructions, missing examples, contradictory constraints, and asking for "
    "several unrelated tasks at once. Each mistake makes the model guess what the "
    "user actually wants, and a guess is where an answer starts to drift away "
    "from what was asked. State the task, the audience, the format and the "
    "constraints plainly, and add an example that matches the desired output."
).replace("a", "").replace("A", "")

# For the OCR tests: a page that exists only as an image.
SCANNED_PAGE = (
    "A Bloom filter answers set membership with no false negatives and a tunable "
    "rate of false positives. It hashes each key with several independent hash "
    "functions and sets the corresponding bits in a fixed bit array. A lookup "
    "reports a key as present only when every one of its bits is set, so adding "
    "more bits per key lowers the false positive rate at the cost of memory."
)

HUGE_PAGE = (
    "The Kestrel reference configuration trains with a global batch size of 4096 "
    "sequences and a peak learning rate of three times ten to the minus four, "
    "warmed up linearly over the first two thousand steps and decayed with a "
    "cosine schedule. Gradient clipping at a norm of one keeps the early steps "
    "stable when the loss is still large."
)


# ── Writers ──────────────────────────────────────────────────────────────────

def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int = 80) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def write_pdf(path: Path, pages: list[str], *, padding_bytes: int = 0,
              encrypted: bool = False) -> None:
    """A minimal, valid PDF with one Helvetica text page per string.

    ``padding_bytes`` adds an unreferenced stream so the file can be made
    larger than any byte limit without adding pages. ``encrypted`` adds a
    Standard security handler whose password is not empty.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")                       # filled in once pages exist
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for text in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
        for line in _wrap(text):
            ops.append(f"({_pdf_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_obj, font, content)
        ))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    if padding_bytes:
        pad = (b"0123456789abcdef" * (padding_bytes // 16 + 1))[:padding_bytes]
        add(b"<< /Length %d >>\nstream\n" % len(pad) + pad + b"\nendstream")
    encrypt_id = None
    if encrypted:
        encrypt_id = add(
            b"<< /Filter /Standard /V 1 /R 2 /P -44 "
            b"/O <" + b"7a" * 32 + b"> /U <" + b"3c" * 32 + b"> >>"
        )

    path.write_bytes(_pdf_bytes(objects, catalog, encrypt_id))


def _pdf_bytes(objects: list[bytes], catalog: int, encrypt_id: int | None = None) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if encrypt_id:
        trailer += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (encrypt_id, b"ab" * 16, b"ab" * 16)
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _render_gray(text: str, dpi: int) -> tuple[int, int, bytes]:
    """(width, height, 8-bit grey pixels) of ``text`` set as a PDF page."""
    tool = shutil.which("pdftoppm")
    if not tool:
        raise RuntimeError("write_scanned_pdf needs pdftoppm on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.pdf"
        write_pdf(src, [text])
        subprocess.run([tool, "-r", str(dpi), "-gray", "-singlefile", "--",  # nosec B603
                        str(src), str(Path(tmp) / "page")], check=True, capture_output=True)
        data = (Path(tmp) / "page.pgm").read_bytes()
    # Binary PGM: "P5", width, height, maxval, one whitespace byte, pixels.
    fields: list[bytes] = []
    pos = 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    width, height = int(fields[1]), int(fields[2])
    return width, height, data[pos + 1:pos + 1 + width * height]


def write_scanned_pdf(path: Path, pages: list[tuple[str, str | None]], *,
                      dpi: int = 200) -> None:
    """A PDF whose pages are IMAGES of text, as a scanner produces.

    Each page is ``(visible, hidden)``: ``visible`` is rendered to pixels, and
    ``hidden``, when given, is laid over it as an invisible text layer (render
    mode 3) — what a text extractor reads instead of the image. A hidden layer
    that has lost a letter reproduces a garbled font on a page whose image is
    perfectly legible. Needs ``pdftoppm``.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for visible, hidden in pages:
        width, height, pixels = _render_gray(visible, dpi)
        packed = zlib.compress(pixels)
        image = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                    b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                    b"/Length %d >>\nstream\n" % (width, height, len(packed))
                    + packed + b"\nendstream")
        ops = ["q 612 0 0 792 0 0 cm /Im1 Do Q"]
        if hidden:
            ops += ["BT", "3 Tr", "/F1 11 Tf", "14 TL", "72 740 Td"]
            ops += [f"({_pdf_escape(line)}) Tj T*" for line in _wrap(hidden)]
            ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> /XObject << /Im1 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_obj, font, image, content)
        ))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    path.write_bytes(_pdf_bytes(objects, catalog))


def write_docx(path: Path, paragraphs: list[str]) -> None:
    """A minimal DOCX: just enough package for ``word/document.xml``."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    # Fixed timestamps: `writestr(name, …)` stamps the current time into each
    # entry, so two builds straddling a two-second boundary produce different
    # bytes — a different sha256, and a "changed file" to the incremental
    # ingest. The tests that re-ingest a rebuilt corpus need identical bytes.
    with zipfile.ZipFile(path, "w") as z:
        for name, data in (("[Content_Types].xml", content_types),
                           ("word/document.xml", document)):
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)


def write_notebook(path: Path, cells: list[tuple[str, str]]) -> None:
    nb = {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [
            {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True),
             **({"outputs": [], "execution_count": None} if kind == "code" else {})}
            for kind, text in cells
        ],
    }
    path.write_text(json.dumps(nb, indent=1), encoding="utf-8")


#: Size of the large PDF. Above self-knowledge's 5 MiB whole-file skip.
HUGE_PDF_BYTES = 6 * 1024 * 1024


def build_corpus(root: Path, *, include_huge: bool = True) -> Path:
    """Write the fixture library under ``root`` and return ``root``."""
    root = Path(root)
    (root / "sql").mkdir(parents=True, exist_ok=True)
    (root / "copies").mkdir(exist_ok=True)
    (root / "__MACOSX").mkdir(exist_ok=True)

    write_pdf(root / "sql" / "SQL_Aggregation_and_Windows.pdf",
              [SQL_PAGE_1, SQL_PAGE_2, SQL_PAGE_3_FIGURE])
    (root / "Statistics_Notes.md").write_text(STATISTICS_MD, encoding="utf-8")
    (root / "RAG_Evaluation_Guide.txt").write_text(RAG_EVAL_TXT, encoding="utf-8")
    # Byte-identical copy under another name: must be indexed once.
    (root / "copies" / "RAG Evaluation Guide (1).txt").write_text(RAG_EVAL_TXT, encoding="utf-8")
    write_docx(root / "LoRA_Finetuning.docx", LORA_DOCX_PARAGRAPHS)
    write_notebook(root / "Pandas_Grouping.ipynb", PANDAS_CELLS)
    (root / "hnsw_index_notes.py").write_text(HNSW_PY, encoding="utf-8")
    (root / "Attention.tex").write_text(ATTENTION_TEX, encoding="utf-8")
    write_pdf(root / "Prompting_Guide.pdf", [PROMPTING_PAGE_1, PROMPTING_PAGE_2_GARBLED])
    write_pdf(root / "Locked_Notes.pdf", ["This text must never be read."], encrypted=True)
    with zipfile.ZipFile(root / "Bundle.zip", "w") as z:
        z.writestr(zipfile.ZipInfo("inner.txt", date_time=(2026, 1, 1, 0, 0, 0)),
                   "archives are skipped, not unpacked")
    (root / "__MACOSX" / "._Statistics_Notes.md").write_bytes(b"\x00\x05\x16\x07")
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    if include_huge:
        write_pdf(root / "Kestrel_Reference.pdf", [HUGE_PAGE], padding_bytes=HUGE_PDF_BYTES)
    return root


#: The fixture's golden questions. Kept as data here rather than committed as
#: a .jsonl file: the P7 truth pass treats any tracked .jsonl as a committed
#: training corpus, and that guard is worth more than a file extension.
GOLDEN = [
    {'question': 'Why does the database reject an aggregate condition in the WHERE clause?', 'expected_title': 'SQL Aggregation and Windows', 'expected_page': 1, 'answerable': True},
    {'question': 'What is the difference between HAVING and WHERE with GROUP BY?', 'expected_title': 'SQL Aggregation and Windows', 'expected_page': 1, 'answerable': True},
    {'question': 'How does DENSE_RANK differ from RANK when there are ties?', 'expected_title': 'SQL Aggregation and Windows', 'expected_page': 2, 'answerable': True},
    {'question': 'How do I write a running total with a window function?', 'expected_title': 'SQL Aggregation and Windows', 'expected_page': 2, 'answerable': True},
    {'question': 'Is a p-value the probability that the null hypothesis is true?', 'expected_title': 'Statistics Notes', 'expected_page': None, 'answerable': True},
    {'question': 'What is statistical power in terms of Type II errors?', 'expected_title': 'Statistics Notes', 'expected_page': None, 'answerable': True},
    {'question': 'How is mean reciprocal rank computed?', 'expected_title': 'RAG Evaluation Guide', 'expected_page': None, 'answerable': True},
    {'question': 'What constant does Reciprocal Rank Fusion usually use?', 'expected_title': 'RAG Evaluation Guide', 'expected_page': None, 'answerable': True},
    {'question': 'Which matrices are trained in LoRA?', 'expected_title': 'LoRA Finetuning', 'expected_page': None, 'answerable': True},
    {'question': 'What quantisation does QLoRA use for the base model?', 'expected_title': 'LoRA Finetuning', 'expected_page': None, 'answerable': True},
    {'question': 'When should I use transform instead of agg in pandas groupby?', 'expected_title': 'Pandas Grouping', 'expected_page': None, 'answerable': True},
    {'question': 'What does the HNSW ef_search parameter control?', 'expected_title': 'hnsw index notes', 'expected_page': None, 'answerable': True},
    {'question': 'Why is attention divided by the square root of the key dimension?', 'expected_title': 'Attention', 'expected_page': None, 'answerable': True},
    {'question': 'What global batch size does the Kestrel reference configuration use?', 'expected_title': 'Kestrel Reference', 'expected_page': 1, 'answerable': True},
    {'question': 'How do I configure horizontal pod autoscaling in Kubernetes?', 'expected_title': None, 'expected_page': None, 'answerable': False},
    {'question': 'What is the boiling point of mercury at sea level?', 'expected_title': None, 'expected_page': None, 'answerable': False},
]


def write_golden(path: Path) -> Path:
    """Write :data:`GOLDEN` as the JSONL the eval harness reads."""
    path = Path(path)
    path.write_text("".join(json.dumps(item) + "\n" for item in GOLDEN), encoding="utf-8")
    return path


__all__ = ["build_corpus", "write_pdf", "write_scanned_pdf", "write_docx", "write_notebook",
           "write_golden", "GOLDEN", "HUGE_PDF_BYTES", "SCANNED_PAGE"]
