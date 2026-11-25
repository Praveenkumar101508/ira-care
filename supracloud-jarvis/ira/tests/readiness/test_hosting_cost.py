"""The hosting cost model (scripts/hosting_cost.py, G4.b) never invents a price.

Prices are inputs, each with a source and a date; a missing price withholds the total. The
arithmetic is checked with synthetic unit prices (1.0 everywhere) that are not quotes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("hosting_cost", ROOT / "scripts/hosting_cost.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def _synthetic(price=1.0):
    return {"items": {k: {"price_gbp": price, "source": "synthetic test value", "read_on": "2026-11-25"} for k in hc.UNIT}}


def test_the_committed_prices_file_invents_nothing():
    prices = json.loads((ROOT / "docs/ops/hosting_prices.json").read_text())
    assert hc.validate(prices) == []
    for entry in prices["items"].values():
        assert entry["price_gbp"] is None or (entry["source"] and entry["read_on"]), entry
    result = hc.monthly("pilot", prices)
    if any(e["price_gbp"] is None for e in prices["items"].values()):
        assert result["total_gbp_per_month"] is None, "a missing price withholds the total"


def test_the_arithmetic_with_unit_prices():
    r = hc.monthly("pilot", _synthetic())
    # one VM at 730 h, plus 100 + 50 + 50 + 1 units a month
    assert r["total_gbp_per_month"] == 730 + 100 + 50 + 50 + 1
    assert r["per_person_gbp_per_month"] == round(931 / 50, 2)
    g = hc.monthly("growth", _synthetic())
    assert g["total_gbp_per_month"] == 2 * 730 + 730 + 200 + 300 + 300 + 1


def test_a_price_without_its_source_is_refused():
    p = _synthetic()
    p["items"]["vm_4vcpu_16gb"]["source"] = None
    assert any("source" in x for x in hc.validate(p))


def test_one_missing_price_withholds_the_total():
    p = _synthetic()
    p["items"]["egress_gb"]["price_gbp"] = None
    r = hc.monthly("pilot", p)
    assert r["total_gbp_per_month"] is None and r["missing"] == ["egress_gb"]
