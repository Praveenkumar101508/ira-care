#!/usr/bin/env python3
"""Monthly hosting cost for an IRA CARE deployment, from a prices file (requirement G4.b).

    python3 scripts/hosting_cost.py docs/ops/hosting_prices.json [--tier pilot|growth]

The sizing is fixed in this file from measurements (docs/ops/HOSTING_PROPOSAL.md); the prices are
inputs. Every price in the prices file must carry its source and the date it was read. A price
left null is reported as missing and the total is withheld. This script never invents a price.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HOURS_PER_MONTH = 730  # the common billing convention (8760 / 12)

#: What each tier needs, from the measured container stack (docs/evidence/staging/containers.json).
TIERS = {
    "pilot": {"people": 50, "items": {"vm_4vcpu_16gb": 1, "block_storage_gb": 100, "object_storage_gb": 50,
                                       "egress_gb": 50, "domain_and_tls": 1}},
    "growth": {"people": 1000, "items": {"vm_4vcpu_16gb": 2, "managed_postgres_2vcpu_8gb": 1,
                                          "block_storage_gb": 200, "object_storage_gb": 300,
                                          "egress_gb": 300, "domain_and_tls": 1}},
}

#: How each item is billed: per hour of running, or per unit per month.
UNIT = {"vm_4vcpu_16gb": "hour", "managed_postgres_2vcpu_8gb": "hour", "block_storage_gb": "month",
        "object_storage_gb": "month", "egress_gb": "month", "domain_and_tls": "month"}


def validate(prices: dict) -> list[str]:
    problems = []
    for item, entry in prices.get("items", {}).items():
        if item not in UNIT:
            problems.append(f"{item}: not an item the sizing uses")
            continue
        if entry.get("price_gbp") is not None:
            if not entry.get("source") or not entry.get("read_on"):
                problems.append(f"{item}: a price needs its source and the date it was read")
            if entry["price_gbp"] < 0:
                problems.append(f"{item}: negative price")
    return problems


def monthly(tier: str, prices: dict) -> dict:
    need = TIERS[tier]["items"]
    lines, missing, total = [], [], 0.0
    for item, qty in need.items():
        entry = prices.get("items", {}).get(item, {})
        p = entry.get("price_gbp")
        if p is None:
            missing.append(item)
            lines.append({"item": item, "quantity": qty, "gbp": None})
            continue
        cost = p * qty * (HOURS_PER_MONTH if UNIT[item] == "hour" else 1)
        total += cost
        lines.append({"item": item, "quantity": qty, "gbp": round(cost, 2), "source": entry.get("source"),
                      "read_on": entry.get("read_on")})
    return {"tier": tier, "people": TIERS[tier]["people"], "lines": lines, "missing": missing,
            "total_gbp_per_month": None if missing else round(total, 2),
            "per_person_gbp_per_month": None if missing else round(total / TIERS[tier]["people"], 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prices")
    ap.add_argument("--tier", choices=sorted(TIERS), default="pilot")
    a = ap.parse_args()
    prices = json.loads(Path(a.prices).read_text())
    bad = validate(prices)
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 2
    print(json.dumps(monthly(a.tier, prices), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
