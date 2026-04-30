"""MCP server that lets the trading agent interact with its environment.

The environment is a DuckDB file, populated externally (not by this server).
Tools expose that environment to the agent: raw rows from the DB, plus
optional technical indicators computed on-the-fly from price history. No
external API calls, no LLM summarization — just structured data in, analysis
performed by the agent itself.
"""

import os
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

DB_PATH = os.environ.get(
    "TRADING_DB_PATH",
    str(Path(__file__).resolve().parent.parent / "env" / "trading_env.duckdb"),
)

mcp = FastMCP("trading_mcp")


def _connect() -> duckdb.DuckDBPyConnection:
    """Open a short-lived read-only connection for each tool call.

    Short-lived connections let the external populator write to the DB without
    being blocked by a long-held reader lock.
    """
    return duckdb.connect(DB_PATH, read_only=True)


@mcp.tool(
    description=(
        "Return SEC filings (10-K, 10-Q) for a symbol in [date_start, date_end] inclusive. "
        "To respect no-look-ahead, pass date_end <= your current target trading day. "
        "For a typical past-year window at a given target date T, use date_start = T - 1 year, date_end = T. "
        "Returns rows of {symbol, date, document_type, mda_content, risk_content}."
    )
)
def get_filings(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    document_type: Annotated[
        Optional[str],
        Field(description="'10-K' or '10-Q'; omit for both"),
    ] = None,
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, "
        "document_type, mda_content, risk_content "
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
        {"symbol": r[0], "date": r[1], "document_type": r[2], "mda_content": r[3], "risk_content": r[4]}
        for r in rows
    ]


@mcp.tool(
    description=(
        "Return news items for a symbol in [date_start, date_end] inclusive. "
        "To respect no-look-ahead, pass date_end <= your current target trading day. "
        "Returns rows of {symbol, date, id, title, highlights}."
    )
)
def get_news(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(DATE(date) AS VARCHAR) AS date, "
        "id, title, highlights "
        "FROM news "
        "WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ? "
        "ORDER BY date ASC, id ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    return [
        {"symbol": r[0], "date": r[1], "id": r[2], "title": r[3], "highlights": r[4]}
        for r in rows
    ]


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


@mcp.tool(
    description=(
        "Return the latest available trading date in the prices table for a symbol. "
        "Use this when no explicit target_date was supplied to the trading skill."
    )
)
def get_latest_date(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT CAST(MAX(date) AS VARCHAR) FROM prices WHERE symbol = ?", [symbol]
        ).fetchone()
    return row[0] if row and row[0] else None


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


if __name__ == "__main__":
    mcp.run()
