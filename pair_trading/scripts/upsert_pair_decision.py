#!/usr/bin/env python3
"""Upsert one pair-trading decision into the run result JSON.

The pair-trading skill invokes this script via Bash so the agent does not have
to write inline Python for file I/O. The script owns load-or-create, filename
sanitization, upsert by date, sorting, date-bound recomputation, and JSON
writing.

Usage:
    python3 pair_trading/scripts/upsert_pair_decision.py \
        --left META --right MSFT --target-date 2025-03-03 \
        --left-price 182.45 --right-price 401.12 \
        --action LONG_SHORT --trajectory "..." --model gpt-5
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("_", value)


def _symbol(value: str) -> str:
    value = value.strip().upper()
    if not value:
        raise argparse.ArgumentTypeError("symbol cannot be empty")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upsert_pair_decision",
        description="Upsert one pair-trading decision into the run result JSON.",
    )
    parser.add_argument("--left", required=True, type=_symbol, help="Left ticker")
    parser.add_argument("--right", required=True, type=_symbol, help="Right ticker")
    parser.add_argument("--target-date", required=True, help="Decision date YYYY-MM-DD")
    parser.add_argument("--left-price", required=True, type=float, help="Current left-leg price")
    parser.add_argument("--right-price", required=True, type=float, help="Current right-leg price")
    parser.add_argument(
        "--action",
        required=True,
        choices=["LONG_SHORT", "SHORT_LONG", "HOLD"],
        help="Pair-trading recommendation",
    )
    parser.add_argument("--trajectory", required=True, help="Brief traceable daily rationale")
    parser.add_argument("--model", required=True, help="Model identifier")
    parser.add_argument(
        "--status",
        default="in_progress",
        choices=["in_progress", "completed", "partial"],
        help="Run status to write (default: in_progress)",
    )
    parser.add_argument(
        "--output-root",
        default="results/pair_trading",
        help="Output directory (default: results/pair_trading)",
    )
    args = parser.parse_args()

    if args.left == args.right:
        parser.error("--left and --right must be distinct symbols")
    try:
        date.fromisoformat(args.target_date)
    except ValueError as exc:
        parser.error(f"--target-date must be YYYY-MM-DD: {exc}")
    if args.left_price <= 0 or args.right_price <= 0:
        parser.error("--left-price and --right-price must be positive")

    model_safe = _sanitize(args.model).lower()
    filename = f"pair_trading_{_sanitize(args.left)}_{_sanitize(args.right)}_{model_safe}.json"
    out_path = Path(args.output_root) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        try:
            doc = json.loads(out_path.read_text())
            if not isinstance(doc, dict) or not isinstance(doc.get("recommendations"), list):
                doc = {"recommendations": []}
        except json.JSONDecodeError:
            doc = {"recommendations": []}
    else:
        doc = {"recommendations": []}

    pair_label = f"{args.left}, {args.right}"
    record = {
        "pair": pair_label,
        "date": args.target_date,
        "price": {
            args.left: float(args.left_price),
            args.right: float(args.right_price),
        },
        "recommended_action": args.action,
        "trajectory": args.trajectory,
    }

    rec_by_date = {
        r["date"]: r
        for r in doc.get("recommendations", [])
        if isinstance(r, dict) and "date" in r
    }
    rec_by_date[args.target_date] = record
    recs = sorted(rec_by_date.values(), key=lambda r: r["date"])

    output = {
        "status": args.status,
        "pair": pair_label,
        "left": args.left,
        "right": args.right,
        "model": args.model,
        "start_date": recs[0]["date"],
        "end_date": recs[-1]["date"],
        "recommendations": recs,
    }
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    print(
        json.dumps(
            {
                "path": str(out_path),
                "pair": pair_label,
                "action_recorded": args.action,
                "date_recorded": args.target_date,
                "total_records": len(recs),
                "start_date": output["start_date"],
                "end_date": output["end_date"],
            }
        )
    )


if __name__ == "__main__":
    main()
