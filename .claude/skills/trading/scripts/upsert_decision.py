#!/usr/bin/env python3
"""Upsert one trading decision into the run's result JSON.

Invoked by the trading skill via Bash so the agent doesn't have to write
inline Python for the write step. The MCP server stays focused on data
lookups; this script owns the file-I/O side (load-or-create, sanitize
filename, upsert by date, sort, recompute start/end, write).

Usage:
    python3 .claude/skills/trading/scripts/upsert_decision.py \
        --symbol TSLA --target-date 2025-03-03 \
        --price 284.65 --action BUY \
        --model claude-sonnet-4-6

Writes to `{output_root}/trading_{symbol}_{model}.json` where `output_root`
defaults to `results/trading` (relative to cwd). Calling again with the same
`--target-date` overwrites that date's record.

Prints one JSON summary line on success:
    {"path": "...", "action_recorded": "BUY", "date_recorded": "...",
     "total_records": N, "start_date": "...", "end_date": "..."}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("_", value)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upsert_decision",
        description="Upsert one trading decision into the run's result JSON.",
    )
    parser.add_argument("--symbol", required=True, help="Stock symbol, e.g. TSLA")
    parser.add_argument("--target-date", required=True, help="Decision date YYYY-MM-DD")
    parser.add_argument("--price", required=True, type=float, help="adj_close for the decision")
    parser.add_argument(
        "--action", required=True, choices=["BUY", "SELL", "HOLD"],
        help="The decision",
    )
    parser.add_argument(
        "--model", required=True,
        help="Your actual model identifier, e.g. claude-sonnet-4-6",
    )
    parser.add_argument(
        "--output-root", default="results/trading",
        help="Output directory (default: results/trading, relative to cwd)",
    )
    args = parser.parse_args()

    try:
        date.fromisoformat(args.target_date)
    except ValueError as exc:
        parser.error(f"--target-date must be YYYY-MM-DD: {exc}")

    filename = (
        f"trading_{_sanitize(args.symbol)}_"
        f"{_sanitize(args.model).lower()}.json"
    )
    out_path = Path(args.output_root) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        try:
            doc = json.loads(out_path.read_text())
            if (
                not isinstance(doc, dict)
                or not isinstance(doc.get("recommendations"), list)
            ):
                doc = {"status": "in_progress", "recommendations": []}
        except json.JSONDecodeError:
            doc = {"status": "in_progress", "recommendations": []}
    else:
        doc = {"status": "in_progress", "recommendations": []}

    rec_by_date = {
        r["date"]: r
        for r in doc["recommendations"]
        if isinstance(r, dict) and "date" in r
    }
    rec_by_date[args.target_date] = {
        "date": args.target_date,
        "price": float(args.price),
        "recommended_action": args.action,
    }
    recs = sorted(rec_by_date.values(), key=lambda r: r["date"])

    doc = {
        "status": "in_progress",
        "symbol": args.symbol,
        "model": args.model,
        "start_date": recs[0]["date"],
        "end_date": recs[-1]["date"],
        "recommendations": recs,
    }
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))

    summary = {
        "path": str(out_path),
        "action_recorded": args.action,
        "date_recorded": args.target_date,
        "total_records": len(recs),
        "start_date": doc["start_date"],
        "end_date": doc["end_date"],
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
