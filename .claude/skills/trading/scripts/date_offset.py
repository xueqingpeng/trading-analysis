#!/usr/bin/env python3
"""Print one or more past-date offsets from a target date.

Usage:
    pythondate_offset.py TARGET_DATE DAYS [DAYS ...]

TARGET_DATE is YYYY-MM-DD. Each DAYS is a non-negative integer: the script
prints `TARGET_DATE - timedelta(days=DAYS)` on its own line, in the same
order as the arguments, formatted as `<days>\\t<YYYY-MM-DD>`.

Example:
    $ python.claude/skills/trading/scripts/date_offset.py 2025-03-26 7 30 365
    7	2025-03-19
    30	2025-02-24
    365	2024-03-26
"""
from __future__ import annotations

import sys
from datetime import date, timedelta


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        target = date.fromisoformat(argv[1])
    except ValueError as e:
        print(f"invalid target date {argv[1]!r}: {e}", file=sys.stderr)
        return 2
    for raw in argv[2:]:
        try:
            days = int(raw)
        except ValueError:
            print(f"invalid days offset {raw!r}: must be an integer", file=sys.stderr)
            return 2
        if days < 0:
            print(f"invalid days offset {raw!r}: must be non-negative", file=sys.stderr)
            return 2
        offset = target - timedelta(days=days)
        print(f"{days}\t{offset.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
