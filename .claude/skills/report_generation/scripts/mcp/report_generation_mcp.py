"""MCP server that lets the report_generation agent interact with its environment.

The environment is a DuckDB file, populated externally (not by this server).
Tools expose that environment to the agent: raw rows from the DB, plus
optional technical indicators computed on-the-fly from price history. No
external API calls, no LLM summarization — just structured data in, analysis
performed by the agent itself.

This server is purpose-built for WEEKLY equity research reports. The agent
is expected to call get_weekly_metrics ONCE per report, getting all 16
required metrics in a single call (11 alpha/momentum + 5 beta block).
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

import os
os.environ.setdefault("NO_COLOR", "1")

# pandas-ta still uses np.NaN, removed in numpy 2.x; restore the alias first.
if not hasattr(np, "NaN"):
    np.NaN = np.nan

import pandas as pd  # noqa: E402
import pandas_ta as _ta  # noqa: E402

DB_PATH: Optional[str] = None
REPORT_GENERATION_DB_PATH_ENV = "REPORT_GENERATION_DB_PATH"
LEGACY_DB_PATH_ENV = "TRADING_DB_PATH"

AS_OF_DATE: Optional[str] = None
REPORT_GENERATION_AS_OF_DATE_ENV = "REPORT_GENERATION_AS_OF_DATE"

PEER_MAP: dict[str, dict] = {
    "AAPL":  {"sector": "Mega-Cap Tech",   "peers": ["MSFT", "GOOGL", "META"]},
    "ADBE":  {"sector": "Software / SaaS", "peers": ["MSFT"]},
    "AMZN":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "META", "MSFT"]},
    "BMRN":  {"sector": "Biotech",         "peers": []},
    "CRM":   {"sector": "Software / SaaS", "peers": ["ADBE", "MSFT"]},
    "GOOGL": {"sector": "Mega-Cap Tech",   "peers": ["META", "AMZN", "MSFT"]},
    "META":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "AMZN"]},
    "MSFT":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "AMZN", "AAPL"]},
    "NVDA":  {"sector": "Mega-Cap Tech / Semiconductors", "peers": ["MSFT", "GOOGL", "META", "AMZN", "AAPL"]},
    "TSLA":  {"sector": "Mega-Cap Tech / EV", "peers": ["AAPL", "AMZN", "META", "GOOGL", "MSFT", "NVDA"]},
}

mcp = FastMCP("report_generation_mcp")


def _get_db_path() -> str:
    if DB_PATH:
        return DB_PATH
    env = os.environ.get(REPORT_GENERATION_DB_PATH_ENV) or os.environ.get(LEGACY_DB_PATH_ENV)
    if env:
        return env
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    default_db = project_root / "env.duckdb"
    if default_db.exists():
        return str(default_db)
    raise RuntimeError(
        "report_generation_mcp: DuckDB path not configured. Pass --db-path=<path> on the "
        f"command line or set {REPORT_GENERATION_DB_PATH_ENV} "
        f"(legacy: {LEGACY_DB_PATH_ENV})."
    )


def _get_as_of_date() -> Optional[str]:
    if AS_OF_DATE:
        return AS_OF_DATE
    return os.environ.get(REPORT_GENERATION_AS_OF_DATE_ENV)


def _clamp_date_end(date_end: str) -> str:
    as_of = _get_as_of_date()
    if as_of and date_end > as_of:
        return as_of
    return date_end


def _check_target_date(target_date: str) -> None:
    as_of = _get_as_of_date()
    if as_of and target_date > as_of:
        raise ValueError(
            f"target_date={target_date} is after as_of_date={as_of}; "
            "look-ahead access is not permitted."
        )


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(_get_db_path(), read_only=True)


def _fetch_adj_close_series(symbol: str, date_start: str, date_end: str) -> pd.Series:
    sql = (
        "SELECT CAST(date AS VARCHAR) AS date, adj_close "
        "FROM prices WHERE symbol = ? AND date >= ? AND date <= ? "
        "ORDER BY date ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["date", "adj_close"])
    return df.set_index("date")["adj_close"].astype(float)


def _compute_beta_block(
    symbol: str,
    peers: list[str],
    target_date: str,
    week_start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> dict:
    null_result = {
        "sector_basket_return_1w_pct": None,
        "relative_return_1w_pct": None,
        "relative_return_4w_pct": None,
        "correlation_60d": None,
        "beta_60d": None,
        "benchmark_basket": [],
    }

    if not peers:
        return null_result

    fetch_start = (date.fromisoformat(target_date) - timedelta(days=100)).isoformat()

    sym_series = _fetch_adj_close_series(symbol, fetch_start, target_date)
    if sym_series.empty:
        return null_result

    peer_series_list = []
    valid_peers = []
    for peer in peers:
        ps = _fetch_adj_close_series(peer, fetch_start, target_date)
        if not ps.empty:
            peer_series_list.append(ps)
            valid_peers.append(peer)

    if not peer_series_list:
        return {**null_result, "benchmark_basket": []}

    peer_df = pd.concat(peer_series_list, axis=1, keys=valid_peers).dropna()
    peer_df_norm = peer_df / peer_df.iloc[0]
    basket_price = peer_df_norm.mean(axis=1)

    sym_aligned = sym_series.reindex(basket_price.index).dropna()
    basket_aligned = basket_price.reindex(sym_aligned.index)

    if len(sym_aligned) < 5:
        return {**null_result, "benchmark_basket": valid_peers}

    week_start_str = week_start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    basket_week = basket_aligned[
        (basket_aligned.index >= week_start_str) & (basket_aligned.index <= end_str)
    ]
    basket_pre_week = basket_aligned[basket_aligned.index < week_start_str]

    sector_basket_return_1w_pct = None
    if len(basket_week) > 0 and len(basket_pre_week) > 0:
        basket_prev_close = float(basket_pre_week.iloc[-1])
        basket_week_close = float(basket_week.iloc[-1])
        if basket_prev_close != 0:
            sector_basket_return_1w_pct = round(
                (basket_week_close - basket_prev_close) / basket_prev_close * 100, 2
            )

    sym_week = sym_aligned[
        (sym_aligned.index >= week_start_str) & (sym_aligned.index <= end_str)
    ]
    sym_pre_week = sym_aligned[sym_aligned.index < week_start_str]

    sym_weekly_return_pct = None
    if len(sym_week) > 0 and len(sym_pre_week) > 0:
        sym_prev_close = float(sym_pre_week.iloc[-1])
        sym_week_close = float(sym_week.iloc[-1])
        if sym_prev_close != 0:
            sym_weekly_return_pct = round(
                (sym_week_close - sym_prev_close) / sym_prev_close * 100, 2
            )

    relative_return_1w_pct = None
    if sym_weekly_return_pct is not None and sector_basket_return_1w_pct is not None:
        relative_return_1w_pct = round(sym_weekly_return_pct - sector_basket_return_1w_pct, 2)

    relative_return_4w_pct = None
    if len(sym_aligned) >= 21 and len(basket_aligned) >= 21:
        sym_idx = list(sym_aligned.index)
        basket_idx = list(basket_aligned.index)
        common_idx = [d for d in sym_idx if d in set(basket_idx)]
        if len(common_idx) >= 21:
            base_date = common_idx[-21]
            sym_4w_base = float(sym_aligned[base_date])
            basket_4w_base = float(basket_aligned[base_date])
            sym_4w_close = float(sym_aligned.iloc[-1])
            basket_4w_close = float(basket_aligned.iloc[-1])
            if sym_4w_base != 0 and basket_4w_base != 0:
                sym_4w_ret = (sym_4w_close - sym_4w_base) / sym_4w_base * 100
                basket_4w_ret = (basket_4w_close - basket_4w_base) / basket_4w_base * 100
                relative_return_4w_pct = round(sym_4w_ret - basket_4w_ret, 2)

    correlation_60d = None
    beta_60d = None

    sym_ret = sym_aligned.pct_change().dropna()
    basket_ret = basket_aligned.pct_change().dropna()
    common_ret_idx = sym_ret.index.intersection(basket_ret.index)

    if len(common_ret_idx) >= 20:
        last_60 = common_ret_idx[-60:] if len(common_ret_idx) >= 60 else common_ret_idx
        s = sym_ret.loc[last_60].astype(float)
        b = basket_ret.loc[last_60].astype(float)
        if len(s) >= 10 and b.std() > 0:
            correlation_60d = round(float(s.corr(b)), 4)
            cov = float(s.cov(b))
            var_b = float(b.var())
            if var_b > 0:
                beta_60d = round(cov / var_b, 4)

    return {
        "sector_basket_return_1w_pct": sector_basket_return_1w_pct,
        "relative_return_1w_pct": relative_return_1w_pct,
        "relative_return_4w_pct": relative_return_4w_pct,
        "correlation_60d": correlation_60d,
        "beta_60d": beta_60d,
        "benchmark_basket": valid_peers,
    }


@mcp.tool(
    description=(
        "Return daily OHLCV prices for a symbol in [date_start, date_end] inclusive. "
        "Returns rows of {symbol, date, open, high, low, close, adj_close, volume}."
    )
)
def get_prices(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    date_end = _clamp_date_end(date_end)
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, "
        "open, high, low, close, adj_close, volume "
        "FROM prices WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    return [
        {"symbol": r[0], "date": r[1], "open": r[2], "high": r[3],
         "low": r[4], "close": r[5], "adj_close": r[6], "volume": r[7]}
        for r in rows
    ]


_INDICATOR_DEFAULT_LENGTH = {"ma": 20, "rsi": 14, "bbands": 20}
_INDICATOR_WARMUP_DAYS = {"ma": 60, "rsi": 60, "bbands": 60, "macd": 120}


@mcp.tool(
    description=(
        "Compute a technical indicator from the prices table. "
        "Supported: 'ma', 'rsi', 'bbands', 'macd'."
    )
)
def get_indicator(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    indicator: Annotated[str, Field(description="One of: 'ma', 'rsi', 'bbands', 'macd'")],
    length: Annotated[Optional[int], Field(description="Window length override; ignored for macd")] = None,
) -> list[dict]:
    date_end = _clamp_date_end(date_end)
    ind = indicator.lower()
    if ind not in {"ma", "rsi", "bbands", "macd"}:
        raise ValueError(f"Unsupported indicator: {indicator!r}.")

    warmup_days = _INDICATOR_WARMUP_DAYS[ind]
    if ind != "macd" and length:
        warmup_days = max(warmup_days, length * 3)
    fetch_start = (date.fromisoformat(date_start) - timedelta(days=warmup_days)).isoformat()

    with _connect() as conn:
        rows = conn.execute(
            "SELECT CAST(date AS VARCHAR) AS date, adj_close AS price FROM prices "
            "WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date ASC",
            [symbol, fetch_start, date_end],
        ).fetchall()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["date", "close"])
    close = df["close"].astype(float)

    if ind == "ma":
        n = length or _INDICATOR_DEFAULT_LENGTH["ma"]
        out = pd.DataFrame({"date": df["date"], "ma": _ta.sma(close, length=n)})
    elif ind == "rsi":
        n = length or _INDICATOR_DEFAULT_LENGTH["rsi"]
        out = pd.DataFrame({"date": df["date"], "rsi": _ta.rsi(close, length=n)})
    elif ind == "bbands":
        n = length or _INDICATOR_DEFAULT_LENGTH["bbands"]
        bb = _ta.bbands(close, length=n)
        out = pd.DataFrame({"date": df["date"], "upper": bb.iloc[:, 2], "middle": bb.iloc[:, 1], "lower": bb.iloc[:, 0]})
    else:
        m = _ta.macd(close)
        out = pd.DataFrame({"date": df["date"], "macd": m.iloc[:, 0], "hist": m.iloc[:, 1], "signal": m.iloc[:, 2]})

    out = out.dropna()
    out = out[(out["date"] >= date_start) & (out["date"] <= date_end)]
    value_cols = [c for c in out.columns if c != "date"]
    out[value_cols] = out[value_cols].round(4)
    return [{"date": r["date"], **{c: float(r[c]) for c in value_cols}} for _, r in out.iterrows()]


@mcp.tool(description="Return compact news metadata for a symbol in [date_start, date_end].")
def list_news(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    date_end = _clamp_date_end(date_end)
    sql = (
        "SELECT symbol, CAST(DATE(date) AS VARCHAR) AS date, id, highlights "
        "FROM news WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ? "
        "ORDER BY date ASC, id ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    return [{"symbol": r[0], "date": r[1], "id": r[2], "highlights": r[3]} for r in rows]


@mcp.tool(description="Fetch a single news article by id.")
def get_news_by_id(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    id: Annotated[int, Field(description="News row id returned by list_news")],
) -> Optional[dict]:
    sql = "SELECT symbol, CAST(DATE(date) AS VARCHAR) AS date, id, highlights FROM news WHERE symbol = ? AND id = ?"
    with _connect() as conn:
        row = conn.execute(sql, [symbol, id]).fetchone()
    if row is None:
        return None
    as_of = _get_as_of_date()
    if as_of and row[1] > as_of:
        return None
    return {"symbol": row[0], "date": row[1], "id": row[2], "highlights": row[3]}


@mcp.tool(description="Return compact filings metadata for a symbol.")
def list_filings(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    document_type: Annotated[Optional[str], Field(description="'10-K' or '10-Q'; omit for both")] = None,
) -> list[dict]:
    date_end = _clamp_date_end(date_end)
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, document_type, "
        "LENGTH(mda_content) AS mda_chars, LENGTH(risk_content) AS risk_chars "
        "FROM filings WHERE symbol = ? AND date >= ? AND date <= ?"
    )
    params: list = [symbol, date_start, date_end]
    if document_type is not None:
        sql += " AND document_type = ?"
        params.append(document_type)
    sql += " ORDER BY date DESC"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"symbol": r[0], "date": r[1], "document_type": r[2], "mda_chars": int(r[3] or 0), "risk_chars": int(r[4] or 0)} for r in rows]


@mcp.tool(description="Fetch one section ('mda' or 'risk') of a specific filing.")
def get_filing_section(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date: Annotated[str, Field(description="Filing date YYYY-MM-DD")],
    document_type: Annotated[str, Field(description="'10-K' or '10-Q'")],
    section: Annotated[str, Field(description="'mda' or 'risk'")],
    offset: Annotated[int, Field(description="0-based start offset in characters", ge=0)] = 0,
    limit: Annotated[Optional[int], Field(description="Max characters to return")] = None,
) -> Optional[dict]:
    section_lc = section.lower()
    col = {"mda": "mda_content", "risk": "risk_content"}.get(section_lc)
    if col is None:
        raise ValueError("section must be 'mda' or 'risk'")
    as_of = _get_as_of_date()
    if limit is None:
        sql = f"SELECT LENGTH({col}), SUBSTRING({col}, ?) FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
        params = [offset + 1, symbol, date, document_type]
    else:
        sql = f"SELECT LENGTH({col}), SUBSTRING({col}, ?, ?) FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
        params = [offset + 1, int(limit), symbol, date, document_type]
    if as_of:
        sql += " AND date <= ?"
        params.append(as_of)
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    total = int(row[0] or 0)
    content = row[1] or ""
    returned = len(content)
    return {"symbol": symbol, "date": date, "document_type": document_type, "section": section_lc,
            "total_chars": total, "offset": offset, "returned_chars": returned,
            "has_more": offset + returned < total, "content": content}


@mcp.tool(description="Determine whether target_date is a US-market trading day.")
def is_trading_day(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    target_date: Annotated[str, Field(description="Target date YYYY-MM-DD")],
) -> dict:
    try:
        target = date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError(f"target_date must be YYYY-MM-DD: {exc}") from exc

    as_of = _get_as_of_date()
    with _connect() as conn:
        if as_of:
            row_today = conn.execute("SELECT adj_close FROM prices WHERE symbol = ? AND date = ? AND date <= ?", [symbol, target_date, as_of]).fetchone()
            row_prev = conn.execute("SELECT CAST(date AS VARCHAR), adj_close FROM prices WHERE symbol = ? AND date < ? AND date <= ? ORDER BY date DESC LIMIT 1", [symbol, target_date, as_of]).fetchone()
            row_latest = conn.execute("SELECT CAST(MAX(date) AS VARCHAR) FROM prices WHERE symbol = ? AND date <= ?", [symbol, as_of]).fetchone()
        else:
            row_today = conn.execute("SELECT adj_close FROM prices WHERE symbol = ? AND date = ?", [symbol, target_date]).fetchone()
            row_prev = conn.execute("SELECT CAST(date AS VARCHAR), adj_close FROM prices WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1", [symbol, target_date]).fetchone()
            row_latest = conn.execute("SELECT CAST(MAX(date) AS VARCHAR) FROM prices WHERE symbol = ?", [symbol]).fetchone()

    latest = row_latest[0] if row_latest and row_latest[0] else None
    prev_day = row_prev[0] if row_prev else None
    prev_close = float(row_prev[1]) if row_prev and row_prev[1] is not None else None

    weekday = target.weekday()
    if weekday >= 5:
        reason, is_td, should_upsert = "weekend", False, True
    elif row_today is not None:
        reason, is_td, should_upsert = "trading_day", True, True
    elif latest is not None and target_date <= latest:
        reason, is_td, should_upsert = "holiday", False, True
    else:
        reason, is_td, should_upsert = "not_loaded", False, False

    return {"symbol": symbol, "date": target_date, "is_trading_day": is_td, "reason": reason,
            "prev_trading_day": prev_day, "prev_trading_day_adj_close": prev_close,
            "latest_date_in_db": latest, "should_upsert": should_upsert}


def _classify_macd(macd_line: float, signal_line: float, hist: float, prev_hist: float) -> str:
    diff = macd_line - signal_line
    eps = max(0.0001 * abs(macd_line), 0.001) if macd_line else 0.001
    if abs(diff) < eps:
        return "neutral"
    if diff > 0:
        return "bullish_strengthening" if hist > prev_hist else "bullish_weakening"
    else:
        return "bearish_strengthening" if hist < prev_hist else "bearish_weakening"


def _classify_rsi(rsi_value: float) -> str:
    if rsi_value > 70:
        return "overbought"
    if rsi_value < 30:
        return "oversold"
    return "neutral"


@mcp.tool(
    description=(
        "Return ALL 16 required weekly metrics for the report in a single call.\n\n"
        "Alpha block (8): week_open, week_close, weekly_return_pct, return_4week_pct,\n"
        "                 ma_20day, price_vs_ma20, weekly_volatility, dist_from_52w_high_pct\n"
        "Momentum block (3): momentum_short, macd_signal, rsi_14\n"
        "Beta block (5): sector_basket_return_1w_pct, relative_return_1w_pct,\n"
        "                relative_return_4w_pct, correlation_60d, beta_60d\n\n"
        "Context extras (not in the 16):\n"
        "  support_20d       — lowest low over trailing 20 trading days (support level)\n"
        "  resistance_20d    — highest high over trailing 20 trading days (resistance level)\n"
        "  cmf_20day         — Chaikin Money Flow over trailing 20 trading days\n"
        "  volume_ratio, ma_5day, ma_60day, week_high, week_low,\n"
        "  dist_from_52w_low_pct, macd_values, rsi_class, week_trading_days, benchmark_basket"
    )
)
def get_weekly_metrics(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    target_date: Annotated[str, Field(description="Last trading day of the report week YYYY-MM-DD")],
) -> dict:
    _check_target_date(target_date)

    sql = (
        "SELECT CAST(date AS VARCHAR) AS date, open, high, low, close, adj_close, volume "
        "FROM prices WHERE symbol = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 260"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, target_date]).fetchall()

    if not rows or rows[0][0] != target_date:
        raise ValueError(f"No price row found for {symbol} on {target_date}.")

    df = pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "adj_close", "volume"]
    ).sort_values("date").reset_index(drop=True)

    end_dt = pd.to_datetime(target_date)
    week_start_dt = end_dt - pd.Timedelta(days=6)
    df_dates = pd.to_datetime(df["date"])
    this_week_mask = (df_dates >= week_start_dt) & (df_dates <= end_dt)
    this_week = df[this_week_mask].reset_index(drop=True)

    if len(this_week) == 0:
        raise ValueError(f"No trading days found in the week ending {target_date} for {symbol}.")

    # ---- Price block ----
    week_open = float(this_week.iloc[0]["open"])
    week_close = float(this_week.iloc[-1]["adj_close"])
    week_high = float(this_week["high"].max())
    week_low = float(this_week["low"].min())

    # ---- Returns block ----
    pre_week = df[~this_week_mask & (df_dates < week_start_dt)]
    if len(pre_week) > 0:
        prev_week_close = float(pre_week.iloc[-1]["adj_close"])
        weekly_return_pct = round((week_close - prev_week_close) / prev_week_close * 100, 2)
    else:
        weekly_return_pct = None

    if len(df) >= 21:
        past_20_close = float(df.iloc[-21]["adj_close"])
        return_4week_pct = round((week_close - past_20_close) / past_20_close * 100, 2)
    else:
        return_4week_pct = None

    # ---- Moving average block ----
    ma_5day = round(float(df["adj_close"].tail(5).mean()), 4) if len(df) >= 5 else None
    ma_20day = round(float(df["adj_close"].tail(20).mean()), 4) if len(df) >= 20 else None
    ma_60day = round(float(df["adj_close"].tail(60).mean()), 4) if len(df) >= 60 else None
    price_vs_ma20 = ("above" if week_close > ma_20day else "below") if ma_20day is not None else None

    # ---- Support & Resistance (20-day high/low) ----
    last_20 = df.tail(20)
    support_20d = round(float(last_20["low"].min()), 4) if len(last_20) >= 1 else None
    resistance_20d = round(float(last_20["high"].max()), 4) if len(last_20) >= 1 else None

    # ---- Chaikin Money Flow (20-day) ----
    # CMF = sum(((close - low) - (high - close)) / (high - low) * volume) / sum(volume)
    cmf_20day = None
    if len(last_20) >= 1:
        hl_range = last_20["high"] - last_20["low"]
        # avoid division by zero on doji candles
        hl_range = hl_range.replace(0, float("nan"))
        mfm = ((last_20["close"] - last_20["low"]) - (last_20["high"] - last_20["close"])) / hl_range
        mfv = mfm * last_20["volume"]
        total_vol = last_20["volume"].sum()
        if total_vol > 0:
            cmf_20day = round(float(mfv.sum() / total_vol), 4)

    # ---- Risk block ----
    this_week_returns = this_week["adj_close"].pct_change().dropna()
    if len(this_week_returns) >= 2:
        weekly_volatility = round(float(this_week_returns.std() * (5 ** 0.5) * 100), 2)
    else:
        weekly_volatility = None

    # ---- Momentum block ----
    if ma_5day is None or ma_20day is None:
        momentum_short = None
    elif ma_5day > ma_20day:
        momentum_short = "up"
    elif ma_5day < ma_20day:
        momentum_short = "down"
    else:
        momentum_short = "neutral"

    macd_signal = None
    macd_values = None
    if len(df) >= 35:
        macd_df = _ta.macd(df["adj_close"].astype(float)).dropna()
        if len(macd_df) >= 2:
            last = macd_df.iloc[-1]
            prev = macd_df.iloc[-2]
            macd_line = float(last.iloc[0])
            macd_hist = float(last.iloc[1])
            macd_sig_line = float(last.iloc[2])
            prev_hist = float(prev.iloc[1])
            macd_signal = _classify_macd(macd_line, macd_sig_line, macd_hist, prev_hist)
            macd_values = {"line": round(macd_line, 4), "signal": round(macd_sig_line, 4), "hist": round(macd_hist, 4)}

    rsi_14 = None
    rsi_class = None
    if len(df) >= 15:
        rsi_series = _ta.rsi(df["adj_close"].astype(float), length=14).dropna()
        if len(rsi_series) > 0:
            rsi_14 = round(float(rsi_series.iloc[-1]), 2)
            rsi_class = _classify_rsi(rsi_14)

    # ---- Volume block ----
    volume_ratio = None
    this_week_avg_vol = float(this_week["volume"].mean())
    pre_window = pre_week.tail(20) if len(pre_week) >= 20 else pre_week
    if len(pre_window) > 0:
        pre_avg_vol = float(pre_window["volume"].mean())
        if pre_avg_vol > 0:
            volume_ratio = round(this_week_avg_vol / pre_avg_vol, 2)

    # ---- Position block ----
    last_252 = df.tail(252)
    high_52w = float(last_252["high"].max())
    low_52w = float(last_252["low"].min())
    dist_from_52w_high_pct = round((week_close - high_52w) / high_52w * 100, 2)
    dist_from_52w_low_pct = round((week_close - low_52w) / low_52w * 100, 2)

    # ---- Beta block ----
    peers = PEER_MAP.get(symbol, {}).get("peers", [])
    beta_block = _compute_beta_block(
        symbol=symbol, peers=peers, target_date=target_date,
        week_start_dt=week_start_dt, end_dt=end_dt,
    )

    return {
        # Alpha block (8)
        "week_open": week_open,
        "week_close": week_close,
        "weekly_return_pct": weekly_return_pct,
        "return_4week_pct": return_4week_pct,
        "ma_20day": ma_20day,
        "price_vs_ma20": price_vs_ma20,
        "weekly_volatility": weekly_volatility,
        "dist_from_52w_high_pct": dist_from_52w_high_pct,
        # Momentum block (3)
        "momentum_short": momentum_short,
        "macd_signal": macd_signal,
        "rsi_14": rsi_14,
        # Beta block (5)
        "sector_basket_return_1w_pct": beta_block["sector_basket_return_1w_pct"],
        "relative_return_1w_pct": beta_block["relative_return_1w_pct"],
        "relative_return_4w_pct": beta_block["relative_return_4w_pct"],
        "correlation_60d": beta_block["correlation_60d"],
        "beta_60d": beta_block["beta_60d"],
        # Context extras
        "support_20d": support_20d,
        "resistance_20d": resistance_20d,
        "cmf_20day": cmf_20day,
        "week_high": week_high,
        "week_low": week_low,
        "ma_5day": ma_5day,
        "ma_60day": ma_60day,
        "dist_from_52w_low_pct": dist_from_52w_low_pct,
        "volume_ratio": volume_ratio,
        "macd_values": macd_values,
        "rsi_class": rsi_class,
        "week_trading_days": int(len(this_week)),
        "benchmark_basket": beta_block["benchmark_basket"],
    }


@mcp.tool(description="Return the top_k most recent news items with highlights pre-fetched.")
def get_news_digest(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    target_date: Annotated[str, Field(description="As-of date YYYY-MM-DD")],
    lookback_days: Annotated[int, Field(description="Days to look back", ge=1)] = 7,
    top_k: Annotated[int, Field(description="Max items to include", ge=1)] = 8,
) -> list[dict]:
    _check_target_date(target_date)
    start = (date.fromisoformat(target_date) - timedelta(days=lookback_days)).isoformat()
    end = _clamp_date_end(target_date)
    sql = (
        "SELECT id, symbol, CAST(DATE(date) AS VARCHAR) AS date, highlights "
        "FROM news WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ? "
        "ORDER BY date DESC LIMIT ?"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, start, end, top_k]).fetchall()
    return [{"id": r[0], "symbol": r[1], "date": r[2], "highlights": r[3]} for r in rows]


@mcp.tool(description="Return MD&A and Risk Factors from the most recent filing.")
def get_filing_highlights(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    target_date: Annotated[str, Field(description="As-of date YYYY-MM-DD")],
    document_type: Annotated[str, Field(description="'10-K' or '10-Q'; empty picks most recent")] = "",
    max_chars: Annotated[int, Field(description="Max characters per content block", ge=100)] = 2500,
) -> dict:
    _check_target_date(target_date)
    if document_type:
        sql = ("SELECT id, CAST(date AS VARCHAR) AS date, document_type, mda_content, risk_content "
               "FROM filings WHERE symbol = ? AND document_type = ? AND date <= ? ORDER BY date DESC LIMIT 1")
        params = [symbol, document_type, target_date]
    else:
        sql = ("SELECT id, CAST(date AS VARCHAR) AS date, document_type, mda_content, risk_content "
               "FROM filings WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1")
        params = [symbol, target_date]
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return {"found": False, "symbol": symbol, "document_type": document_type or "any"}
    _id, fdate, dtype, mda, risk = row
    return {
        "found": True, "symbol": symbol, "filing_id": int(_id),
        "filing_date": fdate, "document_type": dtype,
        "mda_content": (mda or "")[:max_chars],
        "risk_content": (risk or "")[:max_chars],
        "truncated_at": max_chars,
    }


@mcp.tool(description="Return the static peer list and sector label for the given symbol.")
def list_peers(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
) -> dict:
    info = PEER_MAP.get(symbol)
    if not info:
        return {"symbol": symbol, "sector": None, "peers": []}
    return {"symbol": symbol, **info}


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="report_generation_mcp MCP server")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--as-of-date", default=None)
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.db_path:
        DB_PATH = str(Path(args.db_path).expanduser().resolve())
    elif os.environ.get(REPORT_GENERATION_DB_PATH_ENV):
        DB_PATH = os.environ[REPORT_GENERATION_DB_PATH_ENV]
    elif os.environ.get(LEGACY_DB_PATH_ENV):
        DB_PATH = os.environ[LEGACY_DB_PATH_ENV]
    else:
        print(
            f"report_generation_mcp: --db-path is required "
            f"(or set {REPORT_GENERATION_DB_PATH_ENV}; legacy: {LEGACY_DB_PATH_ENV}).",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.as_of_date:
        AS_OF_DATE = args.as_of_date
    elif os.environ.get(REPORT_GENERATION_AS_OF_DATE_ENV):
        AS_OF_DATE = os.environ[REPORT_GENERATION_AS_OF_DATE_ENV]

    mcp.run(transport="stdio")