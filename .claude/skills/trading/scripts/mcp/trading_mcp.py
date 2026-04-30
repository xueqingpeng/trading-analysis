"""MCP server that lets the trading agent interact with its environment.

The environment is a DuckDB file, populated externally (not by this server).
Tools expose that environment to the agent: raw rows from the DB, plus
optional technical indicators computed on-the-fly from price history. No
external API calls, no LLM summarization — just structured data in, analysis
performed by the agent itself.
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Optional

import duckdb
import numpy as np
from fastmcp import FastMCP
from pydantic import Field

# pandas-ta still uses np.NaN, removed in numpy 2.x; restore the alias first.
if not hasattr(np, "NaN"):
    np.NaN = np.nan

import pandas as pd  # noqa: E402
import pandas_ta as _ta  # noqa: E402

# Set by __main__ before mcp.run(). Tool calls resolve the DB path through
# _get_db_path() so importers can also override it programmatically.
DB_PATH: Optional[str] = None

mcp = FastMCP("trading_mcp")


def _get_db_path() -> str:
    if DB_PATH:
        return DB_PATH
    env = os.environ.get("TRADING_DB_PATH")
    if env:
        return env

    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    default_db = project_root / "env.duckdb"
    if default_db.exists():
        return str(default_db)

    raise RuntimeError(
        "trading_mcp: DuckDB path not configured. Pass --db-path=<path> on the "
        "command line or set TRADING_DB_PATH."
    )


def _connect() -> duckdb.DuckDBPyConnection:
    """Open a short-lived read-only connection for each tool call.

    Short-lived connections let the external populator write to the DB without
    being blocked by a long-held reader lock.
    """
    return duckdb.connect(_get_db_path(), read_only=True)


@mcp.tool(
    description=(
        "Return daily OHLCV prices for a symbol in [date_start, date_end] inclusive. "
        "To respect no-look-ahead, pass date_end <= your current target trading day. "
        "Also useful to discover the list of trading days that have data. "
        "Returns rows of {symbol, date, open, high, low, close, adj_close, volume}. "
        "adj_close is the canonical trading price."
    )
)
def get_prices(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, "
        "open, high, low, close, adj_close, volume "
        "FROM prices "
        "WHERE symbol = ? AND date >= ? AND date <= ? "
        "ORDER BY date ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    return [
        {
            "symbol": r[0], "date": r[1],
            "open": r[2], "high": r[3], "low": r[4],
            "close": r[5], "adj_close": r[6], "volume": r[7],
        }
        for r in rows
    ]


_INDICATOR_DEFAULT_LENGTH = {
    "ma": 20,
    "rsi": 14,
    "bbands": 20,
}

_INDICATOR_WARMUP_DAYS = {
    "ma": 60,
    "rsi": 60,
    "bbands": 60,
    "macd": 120,
}


@mcp.tool(
    description=(
        "Compute a technical indicator from the prices table for a symbol over "
        "[date_start, date_end] inclusive. Prices history before date_start is "
        "auto-fetched as warmup. The agent decides which indicators to compute "
        "and when — this tool is optional.\n\n"
        "Supported indicators:\n"
        "  - 'ma'       simple moving average. Default length=20. Returns {date, ma}.\n"
        "  - 'rsi'      relative strength index. Default length=14. Returns {date, rsi}.\n"
        "  - 'bbands'   Bollinger Bands. Default length=20, stddev=2. "
        "Returns {date, upper, middle, lower}.\n"
        "  - 'macd'     MACD with fixed (fast=12, slow=26, signal=9); `length` is "
        "ignored. Returns {date, macd, hist, signal}.\n"
        "\n"
        "To respect no-look-ahead, pass date_end <= your current target trading "
        "day. Values are rounded to 4 decimals."
    )
)
def get_indicator(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    indicator: Annotated[
        str, Field(description="One of: 'ma', 'rsi', 'bbands', 'macd'")
    ],
    length: Annotated[
        Optional[int],
        Field(description="Window length override; ignored for macd"),
    ] = None,
) -> list[dict]:
    ind = indicator.lower()
    if ind not in {"ma", "rsi", "bbands", "macd"}:
        raise ValueError(
            f"Unsupported indicator: {indicator!r}. "
            "Must be one of: ma, rsi, bbands, macd."
        )

    # For variable-length indicators, scale warmup to the requested length.
    warmup_days = _INDICATOR_WARMUP_DAYS[ind]
    if ind != "macd" and length:
        warmup_days = max(warmup_days, length * 3)
    fetch_start = (
        date.fromisoformat(date_start) - timedelta(days=warmup_days)
    ).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            "SELECT CAST(date AS VARCHAR) AS date, adj_close AS price FROM prices "
            "WHERE symbol = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC",
            [symbol, fetch_start, date_end],
        ).fetchall()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["date", "close"])
    close = df["close"].astype(float)

    if ind == "ma":
        n = length or _INDICATOR_DEFAULT_LENGTH["ma"]
        series = _ta.sma(close, length=n)
        out = pd.DataFrame({"date": df["date"], "ma": series})
    elif ind == "rsi":
        n = length or _INDICATOR_DEFAULT_LENGTH["rsi"]
        series = _ta.rsi(close, length=n)
        out = pd.DataFrame({"date": df["date"], "rsi": series})
    elif ind == "bbands":
        n = length or _INDICATOR_DEFAULT_LENGTH["bbands"]
        bb = _ta.bbands(close, length=n)
        out = pd.DataFrame({
            "date": df["date"],
            "upper": bb.iloc[:, 2],
            "middle": bb.iloc[:, 1],
            "lower": bb.iloc[:, 0],
        })
    else:  # macd
        m = _ta.macd(close)
        out = pd.DataFrame({
            "date": df["date"],
            "macd": m.iloc[:, 0],
            "hist": m.iloc[:, 1],
            "signal": m.iloc[:, 2],
        })

    out = out.dropna()
    out = out[(out["date"] >= date_start) & (out["date"] <= date_end)]
    value_cols = [c for c in out.columns if c != "date"]
    out[value_cols] = out[value_cols].round(4)

    return [
        {"date": r["date"], **{c: float(r[c]) for c in value_cols}}
        for _, r in out.iterrows()
    ]


# ----------------------------------------------------------------------
# Fine-grained data tools — avoid returning whole article/filing bodies in
# one shot (which exceeds Claude CLI's inline-result threshold and forces
# the agent to round-trip through tool-results files).
# ----------------------------------------------------------------------


@mcp.tool(
    description=(
        "Return compact news metadata for a symbol in [date_start, date_end] "
        "inclusive: one row per article with "
        "{symbol, date, id, highlights_chars, highlights_preview} — "
        "`highlights_preview` is the first `preview_chars` characters of the "
        "highlights body (default 300), `highlights_chars` is the total length. "
        "Use this FIRST to scan the lead of each day's coverage, then call "
        "`get_news_by_id` to pull the full text of the days that look "
        "relevant. Much cheaper than `get_news` which pulls every article's "
        "full highlights in one call.\n\n"
        "To respect no-look-ahead, pass date_end <= your current target "
        "trading day."
    )
)
def list_news(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    preview_chars: Annotated[
        int,
        Field(
            description="Number of leading characters of `highlights` to return as preview (default 300, max 2000).",
            ge=0,
            le=2000,
        ),
    ] = 300,
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, id, "
        "LENGTH(highlights) AS highlights_chars, "
        "SUBSTRING(highlights, 1, ?) AS preview "
        "FROM news "
        "WHERE symbol = ? AND date >= ? AND date <= ? "
        "ORDER BY date ASC, id ASC"
    )
    with _connect() as conn:
        rows = conn.execute(
            sql, [int(preview_chars), symbol, date_start, date_end]
        ).fetchall()
    return [
        {
            "symbol": r[0], "date": r[1], "id": r[2],
            "highlights_chars": int(r[3] or 0),
            "highlights_preview": r[4] or "",
        }
        for r in rows
    ]


@mcp.tool(
    description=(
        "Fetch a single news article in full by its id. Returns "
        "{symbol, date, id, highlights} or null if not found. "
        "Call this AFTER `list_news` to pull the full highlights text of "
        "the days whose preview you judged relevant."
    )
)
def get_news_by_id(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    id: Annotated[int, Field(description="News row id returned by list_news")],
) -> Optional[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, id, highlights "
        "FROM news WHERE symbol = ? AND id = ?"
    )
    with _connect() as conn:
        row = conn.execute(sql, [symbol, id]).fetchone()
    if row is None:
        return None
    return {
        "symbol": row[0], "date": row[1], "id": row[2],
        "highlights": row[3],
    }


@mcp.tool(
    description=(
        "Return compact filings metadata for a symbol in [date_start, date_end] "
        "inclusive: one row per filing with {symbol, date, document_type, "
        "mda_chars, risk_chars} — the mda_content / risk_content bodies are "
        "NOT included. Use this FIRST to decide which filing and which "
        "section is worth reading, then call `get_filing_section`. Much "
        "cheaper than `get_filings` which pulls every filing's full MD&A "
        "and Risk Factors in one call.\n\n"
        "To respect no-look-ahead, pass date_end <= your current target "
        "trading day."
    )
)
def list_filings(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    document_type: Annotated[
        Optional[str],
        Field(description="'10-K' or '10-Q'; omit for both"),
    ] = None,
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, document_type, "
        "LENGTH(mda_content) AS mda_chars, LENGTH(risk_content) AS risk_chars "
        "FROM filings "
        "WHERE symbol = ? AND date >= ? AND date <= ?"
    )
    params: list = [symbol, date_start, date_end]
    if document_type is not None:
        sql += " AND document_type = ?"
        params.append(document_type)
    sql += " ORDER BY date DESC"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "symbol": r[0], "date": r[1], "document_type": r[2],
            "mda_chars": int(r[3] or 0), "risk_chars": int(r[4] or 0),
        }
        for r in rows
    ]


@mcp.tool(
    description=(
        "Fetch one section ('mda' or 'risk') of a specific filing, with "
        "optional pagination via offset/limit. Returns "
        "{symbol, date, document_type, section, total_chars, offset, "
        "returned_chars, has_more, content} or null if the filing is not "
        "found. Omit `limit` to get the whole section; set offset/limit for "
        "paginated reading of very long sections.\n\n"
        "Example: get_filing_section(symbol='TSLA', date='2024-10-23', "
        "document_type='10-Q', section='mda') → full MD&A text.\n"
        "Example: (..., section='mda', offset=0, limit=20000) → first 20000 "
        "chars with has_more indicating whether to fetch more."
    )
)
def get_filing_section(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date: Annotated[str, Field(description="Filing date YYYY-MM-DD")],
    document_type: Annotated[str, Field(description="'10-K' or '10-Q'")],
    section: Annotated[str, Field(description="'mda' or 'risk'")],
    offset: Annotated[
        int, Field(description="0-based start offset in characters", ge=0),
    ] = 0,
    limit: Annotated[
        Optional[int],
        Field(description="Max characters to return; omit for the entire remainder"),
    ] = None,
) -> Optional[dict]:
    section_lc = section.lower()
    col = {"mda": "mda_content", "risk": "risk_content"}.get(section_lc)
    if col is None:
        raise ValueError("section must be 'mda' or 'risk'")

    # DuckDB SUBSTRING is 1-indexed. With limit omitted, SUBSTRING(col, start)
    # returns everything from `start` to the end.
    if limit is None:
        sql = (
            f"SELECT LENGTH({col}), SUBSTRING({col}, ?) "
            f"FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
        )
        params = [offset + 1, symbol, date, document_type]
    else:
        sql = (
            f"SELECT LENGTH({col}), SUBSTRING({col}, ?, ?) "
            f"FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
        )
        params = [offset + 1, int(limit), symbol, date, document_type]

    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None

    total = int(row[0] or 0)
    content = row[1] or ""
    returned = len(content)
    return {
        "symbol": symbol,
        "date": date,
        "document_type": document_type,
        "section": section_lc,
        "total_chars": total,
        "offset": offset,
        "returned_chars": returned,
        "has_more": offset + returned < total,
        "content": content,
    }


# ----------------------------------------------------------------------
# Trading-day status — single call replaces weekday / missing-row /
# latest-date checks, so the agent doesn't need inline date math.
# ----------------------------------------------------------------------


@mcp.tool(
    description=(
        "Determine whether `date` is a US-market trading day for the given "
        "symbol, and return the previous trading day's adj_close for "
        "convenience. Use this INSTEAD of computing weekday yourself, "
        "checking get_prices for a missing row, and calling get_latest_date. "
        "One call covers all three checks.\n\n"
        "Returns {symbol, date, is_trading_day, reason, prev_trading_day, "
        "prev_trading_day_adj_close, latest_date_in_db, should_upsert}.\n"
        "reason ∈ {'trading_day', 'weekend', 'holiday', 'not_loaded'}.\n"
        "- trading_day: normal trading day with data in DB.\n"
        "- weekend: Saturday or Sunday — force action='HOLD'.\n"
        "- holiday: date <= latest_date_in_db but no price row — force "
        "action='HOLD'.\n"
        "- not_loaded: date > latest_date_in_db — should_upsert=false; "
        "stop and report to user, do not upsert a record."
    )
)
def is_trading_day(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    try:
        target = date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError(f"target_date must be YYYY-MM-DD: {exc}") from exc

    with _connect() as conn:
        row_today = conn.execute(
            "SELECT adj_close FROM prices WHERE symbol = ? AND date = ?",
            [symbol, target_date],
        ).fetchone()
        row_prev = conn.execute(
            "SELECT CAST(date AS VARCHAR), adj_close FROM prices "
            "WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
            [symbol, target_date],
        ).fetchone()
        row_latest = conn.execute(
            "SELECT CAST(MAX(date) AS VARCHAR) FROM prices WHERE symbol = ?",
            [symbol],
        ).fetchone()

    latest = row_latest[0] if row_latest and row_latest[0] else None
    prev_day = row_prev[0] if row_prev else None
    prev_close = float(row_prev[1]) if row_prev and row_prev[1] is not None else None

    weekday = target.weekday()  # 5=Sat, 6=Sun
    if weekday >= 5:
        reason = "weekend"
        is_td = False
        should_upsert = True
    elif row_today is not None:
        reason = "trading_day"
        is_td = True
        should_upsert = True
    elif latest is not None and target_date <= latest:
        reason = "holiday"
        is_td = False
        should_upsert = True
    else:
        reason = "not_loaded"
        is_td = False
        should_upsert = False

    return {
        "symbol": symbol,
        "date": target_date,
        "is_trading_day": is_td,
        "reason": reason,
        "prev_trading_day": prev_day,
        "prev_trading_day_adj_close": prev_close,
        "latest_date_in_db": latest,
        "should_upsert": should_upsert,
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="trading_mcp MCP server")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the DuckDB file. If omitted, falls back to $TRADING_DB_PATH.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.db_path:
        DB_PATH = str(Path(args.db_path).expanduser().resolve())
    elif os.environ.get("TRADING_DB_PATH"):
        DB_PATH = os.environ["TRADING_DB_PATH"]
    else:
        print(
            "trading_mcp: --db-path is required (or set TRADING_DB_PATH).",
            file=sys.stderr,
        )
        sys.exit(2)
    mcp.run()
