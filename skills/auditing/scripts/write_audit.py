#!/usr/bin/env python3
"""Write the final single-line audit JSON for one audit case.

Invoked by the auditing skill via Bash so the agent doesn't have to write
inline Python for the write step. The MCP server stays focused on data
lookups; this script owns the file-I/O side (sanitize filename, ensure
output dir, write the JSON line).

Usage:
    python3 .claude/skills/auditing/scripts/write_audit.py \
        --filing-name 10k --ticker rrr --issue-time 20231231 \
        --concept-id us-gaap:AssetsCurrent --period FY2023 \
        --model claude-sonnet-4-6 \
        --extracted-value -1234567000 --calculated-value 1234567000

Writes to:
    {output_root}/auditing_{filing_name}-{ticker}-{issue_time}_{concept}_{period}_{model}.json

`output_root` defaults to `results/auditing` (relative to cwd). The
concept, period, and model components are sanitized — any character
outside [A-Za-z0-9._-] becomes `-`. Calling again with the same args
**overwrites** the file.

The body is a single JSON object on one line:
    {"extracted_value": "...", "calculated_value": "..."}

Numeric values are written verbatim as strings — no rounding, no reformat.
Pass `"0"` for either field if it cannot be determined.

Prints one JSON summary line on success:
    {"path": "...", "extracted_value": "...", "calculated_value": "...",
     "bytes_written": N}
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("-", value)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="write_audit",
        description="Write the final single-line audit JSON.",
    )
    parser.add_argument("--filing-name", required=True, help="e.g. 10k, 10q (lowercase)")
    parser.add_argument("--ticker", required=True, help="Ticker, lowercase")
    parser.add_argument("--issue-time", required=True, help="Filing issue date YYYYMMDD")
    parser.add_argument(
        "--concept-id", required=True,
        help="Concept QName from the prompt, e.g. us-gaap:AssetsCurrent",
    )
    parser.add_argument(
        "--period", required=True,
        help="Period from the prompt, e.g. 'FY2023', 'Q3 2023', "
        "'2023-12-31', '2023-01-01 to 2023-12-31'",
    )
    parser.add_argument(
        "--model", required=True,
        help="Your actual model identifier, e.g. claude-sonnet-4-6",
    )
    parser.add_argument(
        "--extracted-value", required=True,
        help="Reported value as a numeric string (verbatim from the instance "
        "document); '0' if not found.",
    )
    parser.add_argument(
        "--calculated-value", required=True,
        help="Correct expected value as a numeric string per Case A/B/C/D; "
        "'0' if not determinable.",
    )
    parser.add_argument(
        "--output-root", default="results/auditing",
        help="Output directory (default: results/auditing, relative to cwd)",
    )
    args = parser.parse_args()

    filename = (
        f"auditing_{args.filing_name}-{args.ticker}-{args.issue_time}_"
        f"{_sanitize(args.concept_id)}_{_sanitize(args.period)}_"
        f"{_sanitize(args.model)}.json"
    )
    out_path = Path(args.output_root) / filename
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        parser.error(
            f"Cannot create output directory {out_path.parent}: {exc}. "
            f"Pass --output-root=<writable path> — use whatever directory "
            f"the caller specified in the invocation (e.g. /io/slot1)."
        )

    payload = json.dumps(
        {
            "extracted_value": args.extracted_value,
            "calculated_value": args.calculated_value,
        },
        separators=(", ", ": "),
    )
    data = payload + "\n"
    out_path.write_text(data)

    summary = {
        "path": str(out_path),
        "extracted_value": args.extracted_value,
        "calculated_value": args.calculated_value,
        "bytes_written": len(data.encode()),
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
