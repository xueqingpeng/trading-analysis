#!/usr/bin/env python3
"""Print one or more past-date offsets from a target date.

Usage:
    python3 .claude/skills/hedging/scripts/date_offset.py TARGET_DATE DAYS [DAYS ...]

Each DAYS value is a non-negative integer. Output is one line per offset in
the same order as the arguments, formatted as `<days>\t<YYYY-MM-DD>`.
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
    except ValueError as exc:
        print(f"invalid target date {argv[1]!r}: {exc}", file=sys.stderr)
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
