"""Local MCP tools for the hedging skill.

The environment is either a DuckDB file with the same schema as the
single-stock trading task, or a directory of parquet files populated outside
this server. Tools expose only local data and deterministic pair-position
mechanics: no external API calls and no model summarization happen here.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import duckdb
import numpy as np
import pandas as pd
from fastmcp import FastMCP
from pydantic import Field


DEFAULT_POOL = ["AAPL", "ADBE", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]
DB_PATH: Optional[str] = None
DATA_DIR = Path(
    os.environ.get(
        "HEDGING_DATA_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "trading"),
    )
)
POOL = [
    symbol.strip().upper()
    for symbol in os.environ.get("HEDGING_STOCK_POOL", ",".join(DEFAULT_POOL)).split(",")
    if symbol.strip()
]

mcp = FastMCP("hedging_mcp")


def _default_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "trading" / "env" / "trading_env.duckdb"


def _get_db_path() -> Path:
    if DB_PATH:
        return Path(DB_PATH)
    if os.environ.get("HEDGING_DB_PATH"):
        return Path(os.environ["HEDGING_DB_PATH"])
    return _default_db_path()


def _use_duckdb() -> bool:
    return _get_db_path().exists()


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(_get_db_path()), read_only=True)


def _parquet_path(symbol: str) -> Path:
    return DATA_DIR / f"{symbol.upper()}-00000-of-00001.parquet"


def _jsonable(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _to_list(value) -> list:
    value = _jsonable(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value] if value else []


def _load_symbol(symbol: str) -> pd.DataFrame:
    path = _parquet_path(symbol)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing parquet for {symbol.upper()}: {path}. "
            "Set HEDGING_DATA_DIR if the data lives elsewhere."
        )

    df = pd.read_parquet(path)
    required = {"date", "prices"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    return df.sort_values("date").reset_index(drop=True)


def _validate_symbol(symbol: str) -> str:
    sym = symbol.upper()
    if sym not in POOL:
        raise ValueError(f"Symbol must be in configured pool: {POOL}")
    return sym


def _validate_pair(left: str, right: str) -> tuple[str, str]:
    lft, rgt = _validate_symbol(left), _validate_symbol(right)
    if lft == rgt:
        raise ValueError("left and right must be distinct symbols")
    return lft, rgt


def _validate_range(date_start: str, date_end: str) -> None:
    if date.fromisoformat(date_start) > date.fromisoformat(date_end):
        raise ValueError("date_start must be <= date_end")


def _price_rows_duckdb(symbol: str, date_start: str, date_end: str) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, adj_close AS price, volume "
        "FROM prices "
        "WHERE symbol = ? AND date >= ? AND date <= ? AND adj_close IS NOT NULL "
        "ORDER BY date ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    return [
        {"symbol": r[0], "date": r[1], "price": float(r[2]), "volume": r[3]}
        for r in rows
    ]


def _price_rows_parquet(symbol: str, date_start: str, date_end: str) -> list[dict]:
    df = _load_symbol(symbol)
    mask = (df["date"] >= date_start) & (df["date"] <= date_end)
    out = []
    for _, row in df.loc[mask].iterrows():
        price = row.get("prices")
        if price is None or pd.isna(price):
            continue
        out.append(
            {
                "symbol": symbol,
                "date": row["date"],
                "price": float(price),
                "volume": None if "volume" not in row or pd.isna(row.get("volume")) else row.get("volume"),
            }
        )
    return out


def _price_rows(symbol: str, date_start: str, date_end: str) -> list[dict]:
    _validate_range(date_start, date_end)
    sym = _validate_symbol(symbol)
    if _use_duckdb():
        return _price_rows_duckdb(sym, date_start, date_end)
    return _price_rows_parquet(sym, date_start, date_end)


def _latest_price_date(symbol: str) -> Optional[str]:
    sym = _validate_symbol(symbol)
    if _use_duckdb():
        with _connect() as conn:
            row = conn.execute(
                "SELECT CAST(MAX(date) AS VARCHAR) FROM prices WHERE symbol = ?",
                [sym],
            ).fetchone()
        return row[0] if row and row[0] else None

    rows = _price_rows_parquet(sym, "0001-01-01", "9999-12-31")
    return rows[-1]["date"] if rows else None


def _prev_price(symbol: str, target_date: str) -> Optional[dict]:
    sym = _validate_symbol(symbol)
    if _use_duckdb():
        with _connect() as conn:
            row = conn.execute(
                "SELECT CAST(date AS VARCHAR), adj_close FROM prices "
                "WHERE symbol = ? AND date < ? AND adj_close IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                [sym, target_date],
            ).fetchone()
        if not row:
            return None
        return {"date": row[0], "price": float(row[1])}

    rows = [r for r in _price_rows_parquet(sym, "0001-01-01", target_date) if r["date"] < target_date]
    return {"date": rows[-1]["date"], "price": rows[-1]["price"]} if rows else None


@mcp.tool(description="Return the configured offline stock pool and data location.")
def get_stock_pool() -> dict:
    backend = "duckdb" if _use_duckdb() else "parquet"
    return {
        "pool_name": os.environ.get("HEDGING_POOL_NAME", "offline_tech_pool"),
        "symbols": POOL,
        "backend": backend,
        "db_path": str(_get_db_path()),
        "data_dir": str(DATA_DIR),
        "file_pattern": "{SYMBOL}-00000-of-00001.parquet",
    }


@mcp.tool(
    description=(
        "Return compact daily price rows for a symbol in [date_start, date_end]. "
        "To avoid look-ahead, pass date_end <= the current decision day. "
        "Rows are {symbol, date, price, volume}; price is adj_close for DuckDB."
    )
)
def get_prices(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    return _price_rows(symbol, date_start, date_end)


@mcp.tool(
    description=(
        "Return all dates in [date_start, date_end] where every requested symbol "
        "has a non-null price. Use for calendar discovery, not market signals."
    )
)
def get_common_trading_dates(
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    symbols: Annotated[Optional[list[str]], Field(description="Symbols that must all have prices; defaults to pool")] = None,
) -> list[str]:
    selected = [_validate_symbol(s) for s in (symbols or POOL)]
    date_sets = []
    for symbol in selected:
        date_sets.append({r["date"] for r in _price_rows(symbol, date_start, date_end)})
    if not date_sets:
        return []
    return sorted(set.intersection(*date_sets))


@mcp.tool(
    description=(
        "Return current pair prices on target_date. If either side has no price "
        "that day, returns available=false."
    )
)
def get_pair_prices(
    left: Annotated[str, Field(description="Left ticker in the fixed pair")],
    right: Annotated[str, Field(description="Right ticker in the fixed pair")],
    target_date: Annotated[str, Field(description="Trading date YYYY-MM-DD")],
) -> dict:
    lft, rgt = _validate_pair(left, right)
    left_rows = _price_rows(lft, target_date, target_date)
    right_rows = _price_rows(rgt, target_date, target_date)
    if not left_rows or not right_rows:
        return {"available": False, "date": target_date, "prices": {}}
    return {
        "available": True,
        "date": target_date,
        "prices": {lft: left_rows[0]["price"], rgt: right_rows[0]["price"]},
    }


@mcp.tool(
    description=(
        "Determine whether target_date is tradable for both pair legs. Use this "
        "first for a fixed pair/day instead of computing weekends, checking "
        "missing rows, or finding latest dates yourself. Returns "
        "{left, right, date, is_trading_day, reason, prices, prev_trading_day, "
        "prev_prices, latest_common_date, should_upsert}. reason is one of "
        "{'trading_day','weekend','holiday','missing_leg','not_loaded'}."
    )
)
def is_pair_trading_day(
    left: Annotated[str, Field(description="Left ticker in the fixed pair")],
    right: Annotated[str, Field(description="Right ticker in the fixed pair")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    lft, rgt = _validate_pair(left, right)
    target = date.fromisoformat(target_date)
    today = get_pair_prices(lft, rgt, target_date)
    latest_dates = [_latest_price_date(lft), _latest_price_date(rgt)]
    latest_common = min(d for d in latest_dates if d) if all(latest_dates) else None
    common_before = get_common_trading_dates("0001-01-01", target_date, [lft, rgt])
    prev_day = next((d for d in reversed(common_before) if d < target_date), None)
    prev_prices = {}
    if prev_day:
        prev = get_pair_prices(lft, rgt, prev_day)
        prev_prices = prev.get("prices", {}) if prev.get("available") else {}
    else:
        prev_left = _prev_price(lft, target_date)
        prev_right = _prev_price(rgt, target_date)
        if prev_left and prev_right and prev_left["date"] == prev_right["date"]:
            prev_day = prev_left["date"]
            prev_prices = {lft: prev_left["price"], rgt: prev_right["price"]}

    if target.weekday() >= 5:
        reason = "weekend"
        is_td = False
        should_upsert = True
    elif today["available"]:
        reason = "trading_day"
        is_td = True
        should_upsert = True
    elif latest_common is not None and target_date > latest_common:
        reason = "not_loaded"
        is_td = False
        should_upsert = False
    elif latest_common is not None and target_date <= latest_common:
        left_has = bool(_price_rows(lft, target_date, target_date))
        right_has = bool(_price_rows(rgt, target_date, target_date))
        reason = "holiday" if not left_has and not right_has else "missing_leg"
        is_td = False
        should_upsert = reason == "holiday"
    else:
        reason = "not_loaded"
        is_td = False
        should_upsert = False

    return {
        "left": lft,
        "right": rgt,
        "date": target_date,
        "is_trading_day": is_td,
        "reason": reason,
        "prices": today.get("prices", {}),
        "prev_trading_day": prev_day,
        "prev_prices": prev_prices,
        "latest_common_date": latest_common,
        "should_upsert": should_upsert,
    }


@mcp.tool(
    description=(
        "Return compact news metadata for a symbol in [date_start, date_end]: "
        "{symbol, date, id, highlights_chars, highlights_preview}. "
        "`highlights_preview` is the first `preview_chars` characters of the "
        "highlights body (default 300), `highlights_chars` is the total length. "
        "Use this first to scan the lead of each day's coverage, then call "
        "get_news_by_id for the days whose preview looks relevant."
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
    _validate_range(date_start, date_end)
    sym = _validate_symbol(symbol)
    n = int(preview_chars)
    if _use_duckdb():
        sql = (
            "SELECT symbol, CAST(date AS VARCHAR) AS date, id, "
            "LENGTH(highlights) AS highlights_chars, "
            "SUBSTRING(highlights, 1, ?) AS preview "
            "FROM news "
            "WHERE symbol = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC, id ASC"
        )
        with _connect() as conn:
            rows = conn.execute(sql, [n, sym, date_start, date_end]).fetchall()
        return [
            {
                "symbol": r[0], "date": r[1], "id": str(r[2]),
                "highlights_chars": int(r[3] or 0),
                "highlights_preview": r[4] or "",
            }
            for r in rows
        ]

    df = _load_symbol(sym)
    mask = (df["date"] >= date_start) & (df["date"] <= date_end)
    out = []
    for _, row in df.loc[mask].iterrows():
        for idx, item in enumerate(_to_list(row.get("news"))):
            if isinstance(item, dict):
                highlights = item.get("highlights") or item.get("summary") or item.get("text") or str(item)
            else:
                highlights = str(item)
            out.append({
                "symbol": sym, "date": row["date"],
                "id": f"{sym}|{row['date']}|{idx}",
                "highlights_chars": len(highlights),
                "highlights_preview": highlights[:n],
            })
    return out


@mcp.tool(
    description=(
        "Fetch one news article by id after list_news. Returns "
        "{symbol, date, id, highlights}, or null if not found."
    )
)
def get_news_by_id(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    id: Annotated[str, Field(description="News id returned by list_news")],
) -> Optional[dict]:
    sym = _validate_symbol(symbol)
    if _use_duckdb():
        with _connect() as conn:
            row = conn.execute(
                "SELECT symbol, CAST(date AS VARCHAR) AS date, id, highlights "
                "FROM news WHERE symbol = ? AND CAST(id AS VARCHAR) = ?",
                [sym, str(id)],
            ).fetchone()
        if row is None:
            return None
        return {
            "symbol": row[0],
            "date": row[1],
            "id": str(row[2]),
            "highlights": row[3],
        }

    try:
        _, item_date, raw_idx = str(id).split("|", 2)
        idx = int(raw_idx)
    except ValueError:
        return None
    df = _load_symbol(sym)
    rows = df[df["date"] == item_date]
    if rows.empty:
        return None
    items = _to_list(rows.iloc[0].get("news"))
    if idx < 0 or idx >= len(items):
        return None
    item = items[idx]
    if isinstance(item, dict):
        highlights = item.get("highlights") or item.get("summary") or item.get("text") or str(item)
    else:
        highlights = str(item)
    return {
        "symbol": sym,
        "date": item_date,
        "id": str(id),
        "highlights": highlights,
    }


@mcp.tool(
    description=(
        "Return compact filing metadata for a symbol in [date_start, date_end]: "
        "{symbol, date, document_type, mda_chars, risk_chars}. Section content "
        "is not included. Use get_filing_section for selected sections."
    )
)
def list_filings(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    document_type: Annotated[Optional[str], Field(description="'10-K' or '10-Q'; omit for both")] = None,
) -> list[dict]:
    _validate_range(date_start, date_end)
    sym = _validate_symbol(symbol)
    if _use_duckdb():
        sql = (
            "SELECT symbol, CAST(date AS VARCHAR) AS date, document_type, "
            "LENGTH(mda_content) AS mda_chars, LENGTH(risk_content) AS risk_chars "
            "FROM filings "
            "WHERE symbol = ? AND date >= ? AND date <= ?"
        )
        params: list = [sym, date_start, date_end]
        if document_type is not None:
            sql += " AND document_type = ?"
            params.append(document_type)
        sql += " ORDER BY date DESC"
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {"symbol": r[0], "date": r[1], "document_type": r[2], "mda_chars": int(r[3] or 0), "risk_chars": int(r[4] or 0)}
            for r in rows
        ]

    doc_types = [document_type] if document_type else ["10-K", "10-Q"]
    df = _load_symbol(sym)
    mask = (df["date"] >= date_start) & (df["date"] <= date_end)
    out = []
    for _, row in df.loc[mask].iterrows():
        for doc_type in doc_types:
            col = "10k" if doc_type == "10-K" else "10q"
            text = "\n\n".join(str(item) for item in _to_list(row.get(col)))
            if text:
                out.append({"symbol": sym, "date": row["date"], "document_type": doc_type, "mda_chars": len(text), "risk_chars": 0})
    return sorted(out, key=lambda r: r["date"], reverse=True)


@mcp.tool(
    description=(
        "Fetch one section ('mda' or 'risk') of a specific filing with optional "
        "pagination via offset/limit. Returns {symbol, date, document_type, "
        "section, total_chars, offset, returned_chars, has_more, content}, or "
        "null if not found."
    )
)
def get_filing_section(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date: Annotated[str, Field(description="Filing date YYYY-MM-DD")],
    document_type: Annotated[str, Field(description="'10-K' or '10-Q'")],
    section: Annotated[str, Field(description="'mda' or 'risk'")],
    offset: Annotated[int, Field(description="0-based start offset in characters", ge=0)] = 0,
    limit: Annotated[Optional[int], Field(description="Max characters to return; omit for entire remainder")] = None,
) -> Optional[dict]:
    sym = _validate_symbol(symbol)
    section_lc = section.lower()
    col = {"mda": "mda_content", "risk": "risk_content"}.get(section_lc)
    if col is None:
        raise ValueError("section must be 'mda' or 'risk'")

    if _use_duckdb():
        if limit is None:
            sql = (
                f"SELECT LENGTH({col}), SUBSTRING({col}, ?) "
                "FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
            )
            params = [offset + 1, sym, date, document_type]
        else:
            sql = (
                f"SELECT LENGTH({col}), SUBSTRING({col}, ?, ?) "
                "FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
            )
            params = [offset + 1, int(limit), sym, date, document_type]
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        total = int(row[0] or 0)
        content = row[1] or ""
    else:
        df = _load_symbol(sym)
        rows = df[df["date"] == date]
        if rows.empty:
            return None
        col_name = "10k" if document_type == "10-K" else "10q"
        content = "\n\n".join(str(item) for item in _to_list(rows.iloc[0].get(col_name)))
        if section_lc == "risk":
            content = ""
        total = len(content)
        content = content[offset:] if limit is None else content[offset : offset + int(limit)]

    returned = len(content)
    return {
        "symbol": sym,
        "date": date,
        "document_type": document_type,
        "section": section_lc,
        "total_chars": total,
        "offset": offset,
        "returned_chars": returned,
        "has_more": offset + returned < total,
        "content": content,
    }


@mcp.tool(
    description=(
        "Apply deterministic pair-trading action semantics and dollar-neutral "
        "sizing. LONG_SHORT opens +0.5/left_price and -0.5/right_price. "
        "SHORT_LONG opens -0.5/left_price and +0.5/right_price. HOLD keeps "
        "the existing position. CLOSE returns no position."
    )
)
def apply_pair_action(
    left: Annotated[str, Field(description="Left ticker in the fixed pair")],
    right: Annotated[str, Field(description="Right ticker in the fixed pair")],
    action: Annotated[str, Field(description="One of LONG_SHORT, SHORT_LONG, HOLD, CLOSE")],
    prices: Annotated[dict[str, float], Field(description="Current prices keyed by ticker")],
    current_position: Annotated[Optional[dict], Field(description="Existing position snapshot or null")] = None,
) -> Optional[dict]:
    lft, rgt = _validate_pair(left, right)
    act = action.upper()
    if act == "HOLD":
        return current_position
    if act == "CLOSE":
        return None
    if act not in {"LONG_SHORT", "SHORT_LONG"}:
        raise ValueError("action must be one of LONG_SHORT, SHORT_LONG, HOLD, CLOSE")

    left_price = float(prices[lft])
    right_price = float(prices[rgt])
    if left_price <= 0 or right_price <= 0:
        raise ValueError("Pair prices must be positive to open a position")

    if current_position and current_position.get("direction") == act:
        return current_position

    if act == "LONG_SHORT":
        shares = {lft: 0.5 / left_price, rgt: -0.5 / right_price}
    else:
        shares = {lft: -0.5 / left_price, rgt: 0.5 / right_price}

    return {
        "direction": act,
        "shares": {lft: round(shares[lft], 8), rgt: round(shares[rgt], 8)},
        "entry_prices": {lft: round(left_price, 4), rgt: round(right_price, 4)},
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="hedging_mcp MCP server")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the DuckDB file. If omitted, falls back to $HEDGING_DB_PATH or trading/env/trading_env.duckdb.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Parquet data directory for fallback mode. If omitted, uses $HEDGING_DATA_DIR.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.db_path:
        DB_PATH = str(Path(args.db_path).expanduser().resolve())
    if args.data_dir:
        DATA_DIR = Path(args.data_dir).expanduser().resolve()
    if not _use_duckdb() and not DATA_DIR.exists():
        print(
            "hedging_mcp: no DuckDB found and parquet data directory does not exist. "
            "Pass --db-path, set HEDGING_DB_PATH, or set HEDGING_DATA_DIR.",
            file=sys.stderr,
        )
        sys.exit(2)
    mcp.run()
