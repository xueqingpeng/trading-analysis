#!/usr/bin/env python3
"""Write one report_evaluation JSON artifact into the output directory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(value: str) -> str:
    return _FILENAME_SAFE_RE.sub("_", value)


def main() -> None:
    parser = argparse.ArgumentParser(prog="upsert_evaluation")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-root", default="results/report_evaluation")
    args = parser.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        parser.error("empty evaluation JSON on stdin; nothing to write")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"stdin must be valid JSON: {exc}")

    filename = (
        f"{_sanitize(args.agent).lower()}_report_evaluation_"
        f"{_sanitize(args.ticker)}_{_sanitize(args.model).lower()}.json"
    )
    out_path = Path(args.output_root) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"path": str(out_path)}))


if __name__ == "__main__":
    main()
