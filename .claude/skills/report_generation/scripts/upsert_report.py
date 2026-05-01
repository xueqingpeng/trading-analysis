#!/usr/bin/env python3
"""Upsert one weekly equity research report into the run's result folder.

Invoked by the report_generation skill via Bash so the agent doesn't have to
write inline Python for the write step. The MCP server stays focused on data
lookups; this script owns the file-I/O side (write the Markdown body for the
week, load-or-create the summary JSON, sanitize filename, upsert by date,
sort, recompute start/end, write JSON).

This is the structural twin of `.claude/skills/trading/scripts/upsert_decision.py`:
each skill invocation produces one record, the script owns all persistence.

Usage (report body piped in via stdin heredoc):
    python3 .claude/skills/report_generation/scripts/upsert_report.py \\
        --symbol TSLA --target-date 2025-03-07 \\
        --action BUY \\
        --model claude-sonnet-4-6 \\
        --output-root <dir the caller specified> \\
        <<'REPORT'
    # Weekly Equity Research Report: TSLA
    ...full markdown body...
    REPORT

Writes two artifacts:
  - `{output_root}/report_generation_{symbol}_{model}/report_generation_{symbol}_{YYYYMMDD}_{model}.md`
    (the Markdown body, one file per week)
  - `{output_root}/report_generation_{symbol}_{model}.json`
    (the summary record list, one per run)

where `{output_root}` defaults to `results/report_generation` (relative to
cwd). Calling again with the same `--target-date` overwrites both that
week's `.md` file and the corresponding record in the summary JSON.

Prints one JSON summary line on success:
    {"path": "...", "report_path": "...", "action_recorded": "BUY",
     "date_recorded": "...", "total_records": N, "start_date": "...", "end_date": "..."}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_RATING_CHOICES = ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("_", value)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upsert_report",
        description="Upsert one weekly equity research report into the run's result folder.",
    )
    parser.add_argument("--symbol", required=True, help="Stock symbol, e.g. TSLA")
    parser.add_argument(
        "--target-date", required=True,
        help="Report date (week-ending trading day), YYYY-MM-DD. Used in the filename.",
    )
    parser.add_argument(
        "--action", required=True, choices=_RATING_CHOICES,
        help="The rating token",
    )
    parser.add_argument(
        "--price", type=float, default=0.0,
        help="The week close price (for alignment with trading JSON)",
    )
    parser.add_argument(
        "--model", required=True,
        help="Your actual model identifier, e.g. claude-sonnet-4-6",
    )
    parser.add_argument(
        "--output-root", default="results/report_generation",
        help="Output directory (default: results/report_generation, relative to cwd)",
    )
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.target_date)
    except ValueError as exc:
        parser.error(f"--target-date must be YYYY-MM-DD: {exc}")

    body = sys.stdin.read()
    if not body.strip():
        parser.error("empty report body on stdin; nothing to write")

    # Clean up accidental markdown code block wrappers
    body_stripped = body.strip()
    if body_stripped.startswith("```markdown"):
        body_stripped = body_stripped[11:].strip()
    elif body_stripped.startswith("```"):
        body_stripped = body_stripped[3:].strip()
    if body_stripped.endswith("```"):
        body_stripped = body_stripped[:-3].strip()
    body = body_stripped + "\n"

    symbol_safe = _sanitize(args.symbol)
    model_safe = _sanitize(args.model).lower()

    run_stem = f"report_generation_{symbol_safe}_{model_safe}"
    md_dir = Path(args.output_root) / run_stem
    md_filename = (
        f"report_generation_{symbol_safe}_"
        f"{target_date.strftime('%Y%m%d')}_{model_safe}.md"
    )
    md_path = md_dir / md_filename
    summary_path = Path(args.output_root) / f"{run_stem}.json"

    md_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body, encoding="utf-8", errors="replace")

    # Load-or-create the summary JSON, upsert the record for this week's date.
    if summary_path.exists():
        try:
            doc = json.loads(summary_path.read_text())
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
        "price": args.price,
        "recommended_action": args.action,
        "report_path": str(md_path.relative_to(Path(args.output_root))),
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
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))

    summary = {
        "path": str(summary_path),
        "report_path": str(md_path),
        "action_recorded": args.action,
        "date_recorded": args.target_date,
        "total_records": len(recs),
        "start_date": doc["start_date"],
        "end_date": doc["end_date"],
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
