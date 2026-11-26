"""The dataset and model-weight licence register tells the truth about what ships (item F1).

Reads docs/DATASETS.md and docs/ai/MODEL_WEIGHTS.md. Fails when:
  - a row whose use mentions training has no licence or no verification record;
  - a row used for something that ships says commercial use "no";
  - a row used for something that ships says "unclear" without naming a confirmation or
    permission draft that exists in docs/legal/dataset_permission_requests/;
  - a permission draft carries a contact detail or lacks the placeholder the founders fill in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
DATASETS = ROOT / "docs" / "DATASETS.md"
WEIGHTS = ROOT / "docs" / "ai" / "MODEL_WEIGHTS.md"
DRAFTS = ROOT / "docs" / "legal" / "dataset_permission_requests"
VERIFIED = "Verified (2026-11-25)"
NEEDED = ("Licence", "Commercial use", "Used for", VERIFIED)
DRAFT_PATH = re.compile(r"docs/legal/dataset_permission_requests/([A-Za-z0-9_]+\.md)")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def register_rows(text: str) -> list[dict]:
    """Rows of every table whose header has all the register columns."""
    rows, header = [], None
    for line in text.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = cells if all(n in cells for n in NEEDED) else []
            continue
        if not header or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        if len(cells) != len(header):
            raise ValueError(f"row has {len(cells)} cells, header has {len(header)}: {cells[0]!r}")
        rows.append(dict(zip(header, cells)))
    return rows


def problems(text: str) -> list[str]:
    out = []
    for r in register_rows(text):
        name = r[next(iter(r))]
        use, commercial = r["Used for"].lower(), r["Commercial use"].strip().lower()
        verified, licence = r[VERIFIED].strip(), r["Licence"].strip()
        if commercial not in {"yes", "no", "unclear", "—"}:
            out.append(f"{name}: commercial use must be yes, no or unclear, not {commercial!r}")
        if "training" in use:
            if licence in {"", "—", "-"}:
                out.append(f"{name}: used for training but has no licence")
            if not verified or verified.lower().lstrip("*").startswith("not verified") or verified.lower() == "not repeated":
                out.append(f"{name}: used for training but has no verification record dated 2026-11-25")
        if use.startswith("shipped"):
            if commercial == "no":
                out.append(f"{name}: ships, but commercial use is 'no'")
            if commercial == "unclear":
                drafts = DRAFT_PATH.findall(" ".join(r.values()))
                if not drafts or not all((DRAFTS / d).is_file() for d in drafts):
                    out.append(f"{name}: ships with commercial use 'unclear' and names no existing confirmation draft")
    return out


@pytest.mark.parametrize("path", [DATASETS, WEIGHTS], ids=["datasets", "weights"])
def test_register_rows_are_complete_and_nothing_shipped_is_non_commercial(path: Path):
    text = path.read_text(encoding="utf-8")
    assert len(register_rows(text)) >= 5, f"{path.name}: no register table found"
    assert VERIFIED in text
    assert problems(text) == []


def test_the_datasets_the_owner_named_are_all_in_the_register():
    names = " ".join(next(iter(r.values())) for r in register_rows(DATASETS.read_text(encoding="utf-8")))
    for dataset in ("PPG-DaLiA", "UMAFall", "KFall", "FallAllD", "SisFall", "UCI HAR", "EmpatheticDialogues"):
        assert dataset in names, dataset


def test_the_mandatory_citations_are_kept():
    text = DATASETS.read_text(encoding="utf-8")
    assert "Deep PPG: Large-Scale Heart Rate Estimation with Convolutional Neural Networks" in text
    assert "10.24432/C53890" in text and "10.24432/C54S4K" in text


def test_the_check_notices_a_non_commercial_dataset_in_a_shipped_model():
    text = DATASETS.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| UCI HAR"))
    cells = _cells(row)
    assert cells[4] == "yes"
    cells[4] = "no"
    tampered = text.replace(row, "| " + " | ".join(cells) + " |")
    assert any("commercial use is 'no'" in p for p in problems(tampered))


def test_the_check_notices_a_training_row_without_licence_or_verification():
    text = DATASETS.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| UCI HAR"))
    cells = _cells(row)
    cells[3], cells[6] = "", "not verified: source unreachable from the build environment"
    tampered = text.replace(row, "| " + " | ".join(cells) + " |")
    found = problems(tampered)
    assert any("no licence" in p for p in found) and any("no verification record" in p for p in found)


def test_the_check_notices_an_unclear_shipped_row_without_a_draft():
    text = DATASETS.read_text(encoding="utf-8")
    # SisFall is 'unclear' and, since D-077, no longer in a shipped model: put it back in one,
    # with its confirmation draft missing, and the check must object.
    row = next(line for line in text.splitlines() if line.startswith("| SisFall"))
    cells = _cells(row)
    assert cells[4] == "unclear"
    cells[5] = "shipped model: training (the watch fall model)"
    shipped = "| " + " | ".join(cells) + " |"
    tampered = text.replace(row, DRAFT_PATH.sub("docs/legal/dataset_permission_requests/MISSING.md", shipped))
    assert any("names no existing confirmation draft" in p for p in problems(tampered))


def test_permission_drafts_are_drafts_without_contact_details():
    drafts = sorted(p for p in DRAFTS.glob("*.md") if p.name != "README.md")
    names = {p.name for p in drafts}
    # Only FallAllD is still a draft: SisFall, KFall, UMAFall and WEDA-FALL were sent on 26 November
    # 2026, and their records live beside this folder (docs/legal/*_PERMISSION_REQUEST_2026-11-26.md).
    assert {"FALLALLD.md"} <= names
    for p in drafts:
        text = p.read_text(encoding="utf-8")
        assert "Draft, not sent" in text, p.name
        assert "Praveen Kamineti and Sravani Perni" in text, p.name
        assert "[contact email]" in text, p.name
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), f"{p.name}: no email address in a draft"
        assert not re.search(r"\+?\d[\d ()-]{8,}\d", text.replace("doi:", "")), f"{p.name}: no phone number"
