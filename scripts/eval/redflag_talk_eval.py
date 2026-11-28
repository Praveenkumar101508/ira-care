#!/usr/bin/env python3
"""The talk red-flag layers on every conversation evaluation set (D-114).

    python3 scripts/eval/redflag_talk_eval.py --encoder-dir /path/to/encoder \\
        --out docs/evidence/eval/round10/redflag_talk_layers.json

For each set in `supracloud-jarvis/ira/tests/care/eval/`, every talk turn: the emergencies (expect
red_flag true) caught by the keyword rules alone and by the rules with the talk red-flag head
(`care/models/redflag_talk_head.json`); and the ordinary turns (expect red_flag false) the head would
wrongly flag. The head was trained on an original synthetic corpus only; none of these sets.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IRA = ROOT / "supracloud-jarvis" / "ira"
sys.path.insert(0, str(IRA))

SETS = ("turns", "round9_dev", "round9_holdout", "round10_holdout", "round10_holdout_b", "round10_holdout_c", "round10_holdout_d", "round10_holdout_e", "round10_holdout_f")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--encoder-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    from care.classifier import OnnxEncoder, TalkRedFlagHead
    from care.conversation import red_flag
    head = TalkRedFlagHead.load()
    enc = OnnxEncoder(a.encoder_dir)
    out = {"head": {"threshold": head.threshold, "corpus_sha256": head.corpus_sha256}, "sets": {}}
    for name in SETS:
        rows = [json.loads(l) for l in (IRA / "tests" / "care" / "eval" / f"{name}.jsonl").read_text().splitlines() if l.strip()]
        rows = [r for r in rows if "category" in r and r.get("kind") == "talk"]
        pos = [r for r in rows if r["expect"].get("red_flag")]
        neg = [r for r in rows if r["expect"].get("red_flag") is False]
        p_pos = head.probabilities(enc([r["text"] for r in pos])) if pos else []
        p_neg = head.probabilities(enc([r["text"] for r in neg])) if neg else []
        rules = [red_flag(r["text"]) for r in pos]
        both = [ru or p >= head.threshold for ru, p in zip(rules, p_pos)]
        false_alarms = [r["id"] for r, p in zip(neg, p_neg) if p >= head.threshold and not red_flag(r["text"])]
        out["sets"][name] = {
            "emergencies": len(pos), "caught_by_rules": sum(rules), "caught_by_rules_and_head": sum(both),
            "missed": [r["id"] for r, b in zip(pos, both) if not b],
            "ordinary_turns": len(neg), "head_false_alarms": len(false_alarms), "false_alarm_ids": false_alarms}
        s = out["sets"][name]
        print(f"{name:18} emergencies {s['emergencies']:3}: rules {s['caught_by_rules']:3}, rules+head {s['caught_by_rules_and_head']:3}"
              f" | ordinary {s['ordinary_turns']:3}: head false alarms {s['head_false_alarms']}")
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
