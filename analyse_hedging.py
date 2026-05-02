"""Analyse hedging result JSON and print a detailed report."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def analyse(path: Path) -> None:
    doc = load(path)

    recs = doc["recommendations"]
    left = doc["left"]
    right = doc["right"]
    model = doc["model"]

    print(f"{'='*60}")
    print(f"Pair      : {left} / {right}")
    print(f"Model     : {model}")
    print(f"Date range: {doc['start_date']} → {doc['end_date']}")
    print(f"Records   : {len(recs)}")
    print()

    # ── Action distribution ──────────────────────────────────────────
    counts = Counter(r["recommended_action"] for r in recs)
    total = len(recs)
    print("Action distribution:")
    for action, n in sorted(counts.items()):
        bar = "█" * int(n / total * 40)
        print(f"  {action:<12} {n:>3}  ({n/total*100:5.1f}%)  {bar}")
    print()

    # ── Variation check (Xueqing's concern) ─────────────────────────
    unique_actions = set(counts.keys())
    consecutive_same = max(
        sum(1 for _ in g)
        for _, g in __import__("itertools").groupby(
            r["recommended_action"] for r in recs
        )
    )
    print("Variation check (re Xueqing's concern):")
    print(f"  Unique actions used : {len(unique_actions)} → {unique_actions}")
    print(f"  Longest same-action streak: {consecutive_same} days")
    if len(unique_actions) == 1:
        print("  ⚠️  WARNING: Only one action used — results are all the same!")
    elif consecutive_same > 20:
        print(f"  ⚠️  WARNING: {consecutive_same}-day streak may indicate low variation")
    else:
        print("  ✅ Healthy variation detected")
    print()

    # ── Price ratio over time ────────────────────────────────────────
    print(f"Price ratio ({left}/{right}) over time:")
    print(f"  {'Date':<12} {'Action':<14} {left:>8} {right:>8} {'Ratio':>7}")
    print(f"  {'-'*12} {'-'*14} {'-'*8} {'-'*8} {'-'*7}")
    prev_action = None
    for r in recs:
        lp = r["price"][left]
        rp = r["price"][right]
        ratio = lp / rp
        action = r["recommended_action"]
        marker = " ←" if action != prev_action and prev_action is not None else ""
        print(f"  {r['date']:<12} {action:<14} {lp:>8.2f} {rp:>8.2f} {ratio:>7.4f}{marker}")
        prev_action = action
    print()

    # ── Action flips ─────────────────────────────────────────────────
    flips = [
        (recs[i]["date"], recs[i - 1]["recommended_action"], recs[i]["recommended_action"])
        for i in range(1, len(recs))
        if recs[i]["recommended_action"] != recs[i - 1]["recommended_action"]
    ]
    print(f"Action flips ({len(flips)} total):")
    for date, frm, to in flips:
        print(f"  {date}: {frm} → {to}")
    print()

    # ── Price summary ─────────────────────────────────────────────────
    left_prices  = [r["price"][left]  for r in recs]
    right_prices = [r["price"][right] for r in recs]
    lchg = (left_prices[-1]  - left_prices[0])  / left_prices[0]  * 100
    rchg = (right_prices[-1] - right_prices[0]) / right_prices[0] * 100
    print("Price summary (start → end):")
    print(f"  {left:<6}: ${left_prices[0]:>8.2f} → ${left_prices[-1]:>8.2f}  ({lchg:+.1f}%)")
    print(f"  {right:<6}: ${right_prices[0]:>8.2f} → ${right_prices[-1]:>8.2f}  ({rchg:+.1f}%)")
    print(f"{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        paths = [Path(p) for p in sys.argv[1:]]
    else:
        # Auto-discover in results/hedging/
        paths = sorted(Path("results/hedging").glob("hedging_*.json"))

    if not paths:
        print("No hedging result files found. Pass a path or run from project root.")
        sys.exit(1)

    for p in paths:
        analyse(p)
