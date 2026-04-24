"""Local MCP tools for the pair-trading skill.

The environment is either a DuckDB file with the same schema as the single-stock
trading task, or a directory of parquet files populated outside this server.
Tools expose only local data and deterministic pair-position mechanics: no Yahoo
Finance, Exa, SEC API, or model calls happen here.
"""

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import duckdb
import numpy as np
import pandas as pd
from fastmcp import FastMCP
from pydantic import Field


DEFAULT_POOL = ["AAPL", "ADBE", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]
DB_PATH = Path(
    os.environ.get(
        "PAIR_TRADING_DB_PATH",
        str(Path(__file__).resolve().parents[2] / "trading" / "env" / "trading_env.duckdb"),
    )
)
DATA_DIR = Path(
    os.environ.get(
        "PAIR_TRADING_DATA_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "trading"),
    )
)
POOL = [
    symbol.strip().upper()
    for symbol in os.environ.get("PAIR_TRADING_STOCK_POOL", ",".join(DEFAULT_POOL)).split(",")
    if symbol.strip()
]

mcp = FastMCP("pair_trading_mcp")


def _use_duckdb() -> bool:
    return DB_PATH.exists()


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


def _parquet_path(symbol: str) -> Path:
    return DATA_DIR / f"{symbol.upper()}-00000-of-00001.parquet"


def _to_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return [str(value)] if value else []


def _jsonable(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if not isinstance(value, (list, tuple, dict, np.ndarray)) and pd.isna(value):
        return None
    return value


def _load_symbol(symbol: str) -> pd.DataFrame:
    path = _parquet_path(symbol)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing parquet for {symbol.upper()}: {path}. "
            "Set PAIR_TRADING_DATA_DIR if the data lives elsewhere."
        )

    df = pd.read_parquet(path)
    required = {"date", "prices"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _rows_for_symbol_duckdb(symbol: str, date_start: str, date_end: str) -> list[dict]:
    sql = """
        WITH price_rows AS (
            SELECT
                symbol,
                CAST(date AS VARCHAR) AS date,
                adj_close AS price,
                volume
            FROM prices
            WHERE symbol = ? AND date >= ? AND date <= ?
        ),
        news_rows AS (
            SELECT
                symbol,
                CAST(DATE(date) AS VARCHAR) AS date,
                list({title: title, highlights: highlights}) AS news
            FROM news
            WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ?
            GROUP BY symbol, CAST(DATE(date) AS VARCHAR)
        ),
        filing_rows AS (
            SELECT
                symbol,
                CAST(date AS VARCHAR) AS date,
                list(CASE WHEN document_type = '10-K' THEN {mda_content: mda_content, risk_content: risk_content} ELSE NULL END) AS ten_k,
                list(CASE WHEN document_type = '10-Q' THEN {mda_content: mda_content, risk_content: risk_content} ELSE NULL END) AS ten_q
            FROM filings
            WHERE symbol = ? AND date >= ? AND date <= ?
            GROUP BY symbol, CAST(date AS VARCHAR)
        )
        SELECT
            p.symbol,
            p.date,
            p.price,
            p.volume,
            coalesce(n.news, []) AS news,
            coalesce(f.ten_k, []) AS ten_k,
            coalesce(f.ten_q, []) AS ten_q
        FROM price_rows p
        LEFT JOIN news_rows n ON p.symbol = n.symbol AND p.date = n.date
        LEFT JOIN filing_rows f ON p.symbol = f.symbol AND p.date = f.date
        WHERE p.price IS NOT NULL
        ORDER BY p.date ASC
    """
    with _connect() as conn:
        rows = conn.execute(
            sql,
            [symbol.upper(), date_start, date_end] * 3,
        ).fetchall()
    return [
        {
            "symbol": r[0],
            "date": r[1],
            "price": float(r[2]),
            "volume": r[3],
            "news": [item for item in _to_list(r[4]) if item is not None],
            "10k": [item for item in _to_list(r[5]) if item is not None],
            "10q": [item for item in _to_list(r[6]) if item is not None],
            "momentum": None,
        }
        for r in rows
    ]


def _rows_for_symbol_parquet(symbol: str, date_start: str, date_end: str) -> list[dict]:
    if date.fromisoformat(date_start) > date.fromisoformat(date_end):
        raise ValueError("date_start must be <= date_end")

    df = _load_symbol(symbol)
    mask = (df["date"] >= date_start) & (df["date"] <= date_end)
    out = []
    for _, row in df.loc[mask].iterrows():
        price = row.get("prices")
        if price is None or pd.isna(price):
            continue
        out.append(
            {
                "symbol": symbol.upper(),
                "date": row["date"],
                "price": float(price),
                "news": _to_list(row.get("news")),
                "10k": _to_list(row.get("10k")),
                "10q": _to_list(row.get("10q")),
                "momentum": None if pd.isna(row.get("momentum")) else str(row.get("momentum")),
            }
        )
    return out


def _rows_for_symbol(symbol: str, date_start: str, date_end: str) -> list[dict]:
    if date.fromisoformat(date_start) > date.fromisoformat(date_end):
        raise ValueError("date_start must be <= date_end")
    if _use_duckdb():
        return _rows_for_symbol_duckdb(symbol, date_start, date_end)
    return _rows_for_symbol_parquet(symbol, date_start, date_end)


def _validate_pair(left: str, right: str) -> tuple[str, str]:
    lft, rgt = left.upper(), right.upper()
    if lft == rgt:
        raise ValueError("left and right must be distinct symbols")
    if lft not in POOL or rgt not in POOL:
        raise ValueError(f"Pair must use symbols from the configured pool: {POOL}")
    return lft, rgt


@mcp.tool(description="Return the configured offline stock pool and parquet data directory.")
def get_stock_pool() -> dict:
    backend = "duckdb" if _use_duckdb() else "parquet"
    return {
        "pool_name": os.environ.get("PAIR_TRADING_POOL_NAME", "offline_tech_pool"),
        "symbols": POOL,
        "backend": backend,
        "db_path": str(DB_PATH),
        "data_dir": str(DATA_DIR),
        "file_pattern": "{SYMBOL}-00000-of-00001.parquet",
    }


@mcp.tool(
    description=(
        "Return rows for every stock in the pool over [date_start, date_end]. "
        "Use this on the first trading day to select one pair without future data. "
        "Rows contain {symbol, date, price, news, 10k, 10q, momentum}."
    )
)
def get_pair_selection_context(
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD; must not exceed the selection date")],
    symbols: Annotated[Optional[list[str]], Field(description="Optional subset of pool symbols")] = None,
) -> dict:
    selected = [s.upper() for s in (symbols or POOL)]
    invalid = [s for s in selected if s not in POOL]
    if invalid:
        raise ValueError(f"Symbols not in configured pool: {invalid}")

    return {
        "date_start": date_start,
        "date_end": date_end,
        "symbols": selected,
        "context": {symbol: _rows_for_symbol(symbol, date_start, date_end) for symbol in selected},
    }


@mcp.tool(
    description=(
        "Return local market context for a fixed pair over [date_start, date_end]. "
        "Use this for daily pair decisions, always with date_end <= the current trading day."
    )
)
def get_pair_market_context(
    left: Annotated[str, Field(description="Left ticker in the fixed pair")],
    right: Annotated[str, Field(description="Right ticker in the fixed pair")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD; no look-ahead")],
) -> dict:
    lft, rgt = _validate_pair(left, right)
    return {
        "pair": {"left": lft, "right": rgt, "label": f"{lft},{rgt}"},
        "date_start": date_start,
        "date_end": date_end,
        "rows": {
            lft: _rows_for_symbol(lft, date_start, date_end),
            rgt: _rows_for_symbol(rgt, date_start, date_end),
        },
    }


@mcp.tool(
    description=(
        "Return the current close prices for both legs on target_date. "
        "If either side has no price on that date, returns available=false."
    )
)
def get_pair_prices(
    left: Annotated[str, Field(description="Left ticker in the fixed pair")],
    right: Annotated[str, Field(description="Right ticker in the fixed pair")],
    target_date: Annotated[str, Field(description="Trading date YYYY-MM-DD")],
) -> dict:
    lft, rgt = _validate_pair(left, right)
    left_rows = _rows_for_symbol(lft, target_date, target_date)
    right_rows = _rows_for_symbol(rgt, target_date, target_date)
    if not left_rows or not right_rows:
        return {"available": False, "date": target_date, "prices": {}}
    return {
        "available": True,
        "date": target_date,
        "prices": {
            lft: left_rows[0]["price"],
            rgt: right_rows[0]["price"],
        },
    }


@mcp.tool(
    description=(
        "Return all dates in [date_start, date_end] where every requested symbol has a non-null price."
    )
)
def get_common_trading_dates(
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    symbols: Annotated[Optional[list[str]], Field(description="Symbols that must all have prices; defaults to pool")] = None,
) -> list[str]:
    selected = [s.upper() for s in (symbols or POOL)]
    date_sets = []
    for symbol in selected:
        rows = _rows_for_symbol(symbol, date_start, date_end)
        date_sets.append({r["date"] for r in rows})
    if not date_sets:
        return []
    return sorted(set.intersection(*date_sets))


@mcp.tool(
    description=(
        "Apply the old pair-trading action semantics and dollar-neutral sizing. "
        "LONG_SHORT opens +0.5/left_price and -0.5/right_price. "
        "SHORT_LONG opens -0.5/left_price and +0.5/right_price. "
        "HOLD keeps the existing position. CLOSE returns no position."
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


if __name__ == "__main__":
    mcp.run()
