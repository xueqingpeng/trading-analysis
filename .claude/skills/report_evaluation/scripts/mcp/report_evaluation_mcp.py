"""MCP server for the report_evaluation skill.

This server exposes data the evaluator needs to score generated WEEKLY reports:
- offline market/news/filings data from a DuckDB file
- generated Markdown reports from the report_generation results directory
- evaluation-specific helpers (forward returns, metric verification, leakage
  detection, structured report parsing)
- mirrors of report_generation_mcp's pre-packaging helpers so the evaluator
  computes ground truth using the SAME logic the generator did

Critical design note — NO as_of_date guard on this server:
  Evaluation is intentionally retrospective. To compute forward returns and
  verify whether a rating played out, the evaluator MUST be able to read data
  past report_date. The skill prompt enforces "use only <= report_date for
  per-report scoring; use forward data only for backtest aggregation"; the
  MCP layer leaves all data accessible.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Optional

import duckdb
import numpy as np
from fastmcp import FastMCP
from pydantic import Field

if not hasattr(np, "NaN"):
    np.NaN = np.nan

import pandas as pd  # noqa: E402
import pandas_ta as _ta  # noqa: E402

DB_PATH: Optional[str] = None
REPORTS_ROOT: Optional[str] = None

REPORT_EVALUATION_DB_PATH_ENV = "REPORT_EVALUATION_DB_PATH"
LEGACY_DB_PATH_ENV = "TRADING_DB_PATH"
REPORT_EVALUATION_REPORTS_ROOT_ENV = "REPORT_EVALUATION_REPORTS_ROOT"

# Numeric mapping for the 5 allowed rating tokens. Used by get_report_metrics
# and downstream backtest aggregation in the skill prompt.
RATING_TO_NUMERIC: dict[str, int] = {
    "STRONG_SELL": -2,
    "SELL": -1,
    "HOLD": 0,
    "BUY": 1,
    "STRONG_BUY": 2,
}

# Must stay aligned with report_generation_mcp.PEER_MAP. The evaluator uses
# this to recompute the same beta / relative-performance block that the
# generator saw at report time.
PEER_MAP: dict[str, dict] = {
    "AAPL":  {"sector": "Mega-Cap Tech",   "peers": ["MSFT", "GOOGL", "META"]},
    "ADBE":  {"sector": "Software / SaaS", "peers": ["MSFT"]},
    "AMZN":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "META", "MSFT"]},
    "GOOGL": {"sector": "Mega-Cap Tech",   "peers": ["META", "AMZN", "MSFT"]},
    "META":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "AMZN"]},
    "MSFT":  {"sector": "Mega-Cap Tech",   "peers": ["GOOGL", "AMZN", "AAPL"]},
    "NVDA":  {"sector": "Mega-Cap Tech / Semiconductors", "peers": ["MSFT", "GOOGL", "META", "AMZN", "AAPL"]},
    "TSLA":  {"sector": "Mega-Cap Tech / EV", "peers": ["AAPL", "AMZN", "META", "GOOGL", "MSFT", "NVDA"]},
}

mcp = FastMCP("report_evaluation_mcp")


def _get_db_path() -> str:
    if DB_PATH:
        return DB_PATH
    env = os.environ.get(REPORT_EVALUATION_DB_PATH_ENV) or os.environ.get(
        LEGACY_DB_PATH_ENV
    )
    if env:
        return env

    project_root = Path(__file__).resolve().parents[5]
    default_db = project_root / "env.duckdb"
    if default_db.exists():
        return str(default_db)

    raise RuntimeError(
        "report_evaluation_mcp: DuckDB path not configured. Pass --db-path=<path> "
        f"or set {REPORT_EVALUATION_DB_PATH_ENV} (legacy: {LEGACY_DB_PATH_ENV})."
    )


def _get_reports_root() -> Path:
    root = REPORTS_ROOT or os.environ.get(REPORT_EVALUATION_REPORTS_ROOT_ENV)
    if not root:
        raise RuntimeError(
            "report_evaluation_mcp: reports root not configured. Pass "
            f"--reports-root=<path> or set {REPORT_EVALUATION_REPORTS_ROOT_ENV}."
        )
    path = Path(root).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Reports root does not exist: {path}")
    return path


def _connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(_get_db_path(), read_only=True)


def _fetch_adj_close_series(symbol: str, date_start: str, date_end: str) -> pd.Series:
    """Return an adj_close Series indexed by date string, matching generation MCP."""
    sql = (
        "SELECT CAST(date AS VARCHAR) AS date, adj_close "
        "FROM prices WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date ASC"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, date_start, date_end]).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series(
        data=[float(r[1]) for r in rows],
        index=[str(r[0]) for r in rows],
        dtype=float,
        name=symbol,
    )


def _compute_beta_block(
    symbol: str,
    peers: list[str],
    target_date: str,
    week_start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> dict:
    """Mirror report_generation_mcp._compute_beta_block exactly."""
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

    basket_week = basket_aligned[(basket_aligned.index >= week_start_str) & (basket_aligned.index <= end_str)]
    basket_pre_week = basket_aligned[basket_aligned.index < week_start_str]
    sector_basket_return_1w_pct = None
    if len(basket_week) > 0 and len(basket_pre_week) > 0:
        basket_prev_close = float(basket_pre_week.iloc[-1])
        basket_week_close = float(basket_week.iloc[-1])
        if basket_prev_close != 0:
            sector_basket_return_1w_pct = round((basket_week_close - basket_prev_close) / basket_prev_close * 100, 2)

    sym_week = sym_aligned[(sym_aligned.index >= week_start_str) & (sym_aligned.index <= end_str)]
    sym_pre_week = sym_aligned[sym_aligned.index < week_start_str]
    sym_weekly_return_pct = None
    if len(sym_week) > 0 and len(sym_pre_week) > 0:
        sym_prev_close = float(sym_pre_week.iloc[-1])
        sym_week_close = float(sym_week.iloc[-1])
        if sym_prev_close != 0:
            sym_weekly_return_pct = round((sym_week_close - sym_prev_close) / sym_prev_close * 100, 2)

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


# ---------------------------------------------------------------------------
# Market / news / filings tools — same as before. NO as_of_date guard:
# evaluation must be able to read data past report_date in order to compute
# forward returns and verify outcomes.
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Return daily OHLCV prices for a symbol in [date_start, date_end] inclusive. "
        "Use this to recompute expected metrics and simulate report ratings."
    )
)
def get_prices(
    symbol: Annotated[str, Field(description="Stock symbol, e.g. 'AAPL'")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, open, high, low, close, adj_close, volume "
        "FROM prices WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date ASC"
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
        "Return compact news metadata for a symbol in [date_start, date_end] "
        "inclusive: one row per article with "
        "{symbol, date, id, highlights_chars, highlights_preview} — "
        "`highlights_preview` is the first `preview_chars` characters of the "
        "highlights body (default 600 — weekly evaluation tolerates more lead "
        "text than daily trading), `highlights_chars` is the total length. "
        "Use this FIRST to scan the lead of each day's coverage, then call "
        "`get_news_by_id` to pull the full text of items worth verifying."
    )
)
def list_news(
    symbol: Annotated[str, Field(description="Stock symbol")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    preview_chars: Annotated[
        int,
        Field(
            description="Number of leading characters of `highlights` to return as preview (default 600, max 2000).",
            ge=0,
            le=2000,
        ),
    ] = 600,
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(DATE(date) AS VARCHAR) AS date, id, "
        "LENGTH(highlights) AS highlights_chars, "
        "SUBSTRING(highlights, 1, ?) AS preview "
        "FROM news WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ? "
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
        "Call this AFTER `list_news` to pull "
        "the full highlights text of items whose preview you judged relevant."
    )
)
def get_news_by_id(
    symbol: Annotated[str, Field(description="Stock symbol")],
    id: Annotated[int, Field(description="News id returned by list_news")],
) -> Optional[dict]:
    sql = (
        "SELECT symbol, CAST(DATE(date) AS VARCHAR) AS date, id, highlights "
        "FROM news WHERE symbol = ? AND id = ?"
    )
    with _connect() as conn:
        row = conn.execute(sql, [symbol, id]).fetchone()
    if row is None:
        return None
    return {
        "symbol": row[0], "date": row[1], "id": row[2], "highlights": row[3],
    }


@mcp.tool(
    description=(
        "Return compact filings metadata for a symbol in [date_start, date_end] inclusive: "
        "{symbol, date, document_type, mda_chars, risk_chars}."
    )
)
def list_filings(
    symbol: Annotated[str, Field(description="Stock symbol")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    document_type: Annotated[
        Optional[str], Field(description="'10-K' or '10-Q'; omit for both")
    ] = None,
) -> list[dict]:
    sql = (
        "SELECT symbol, CAST(date AS VARCHAR) AS date, document_type, "
        "LENGTH(COALESCE(mda_content, '')), LENGTH(COALESCE(risk_content, '')) "
        "FROM filings WHERE symbol = ? AND date >= ? AND date <= ?"
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
        "Fetch one filing section for a specific filing. section must be 'mda' or 'risk'. "
        "Supports pagination via offset/limit."
    )
)
def get_filing_section(
    symbol: Annotated[str, Field(description="Stock symbol")],
    date: Annotated[str, Field(description="Filing date YYYY-MM-DD")],
    document_type: Annotated[str, Field(description="'10-K' or '10-Q'")],
    section: Annotated[str, Field(description="'mda' or 'risk'")],
    offset: Annotated[int, Field(description="Character offset", ge=0)] = 0,
    limit: Annotated[Optional[int], Field(description="Optional char limit", gt=0)] = None,
) -> Optional[dict]:
    section_lc = section.lower()
    if section_lc not in {"mda", "risk"}:
        raise ValueError("section must be 'mda' or 'risk'")
    col = "mda_content" if section_lc == "mda" else "risk_content"
    if limit is None:
        sql = f"SELECT LENGTH({col}), SUBSTRING({col}, ?) FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
        params = [offset + 1, symbol, date, document_type]
    else:
        sql = f"SELECT LENGTH({col}), SUBSTRING({col}, ?, ?) FROM filings WHERE symbol = ? AND date = ? AND document_type = ?"
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


@mcp.tool(
    description=(
        "Compute a technical indicator over [date_start, date_end]. Supported indicators: "
        "ma, rsi, bbands, macd. Values rounded to 4 decimals."
    )
)
def get_indicator(
    symbol: Annotated[str, Field(description="Stock symbol")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    indicator: Annotated[str, Field(description="One of: ma, rsi, bbands, macd")],
    length: Annotated[
        Optional[int],
        Field(description="Window length override; ignored for macd"),
    ] = None,
) -> list[dict]:
    ind = indicator.lower()
    if ind not in {"ma", "rsi", "bbands", "macd"}:
        raise ValueError("indicator must be one of: ma, rsi, bbands, macd")
    warmup_days = {"ma": 60, "rsi": 60, "bbands": 60, "macd": 120}[ind]
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
        n = length or 20
        out = pd.DataFrame({"date": df["date"], "ma": _ta.sma(close, length=n)})
    elif ind == "rsi":
        n = length or 14
        out = pd.DataFrame({"date": df["date"], "rsi": _ta.rsi(close, length=n)})
    elif ind == "bbands":
        n = length or 20
        bb = _ta.bbands(close, length=n)
        out = pd.DataFrame({
            "date": df["date"], "upper": bb.iloc[:, 2],
            "middle": bb.iloc[:, 1], "lower": bb.iloc[:, 0],
        })
    else:
        m = _ta.macd(close)
        out = pd.DataFrame({
            "date": df["date"], "macd": m.iloc[:, 0],
            "hist": m.iloc[:, 1], "signal": m.iloc[:, 2],
        })
    out = out.dropna()
    out = out[(out["date"] >= date_start) & (out["date"] <= date_end)]
    value_cols = [c for c in out.columns if c != "date"]
    out[value_cols] = out[value_cols].round(4)
    return [
        {"date": r["date"], **{c: float(r[c]) for c in value_cols}}
        for _, r in out.iterrows()
    ]


# ---------------------------------------------------------------------------
# Reports tools — discover and read the markdown files produced by
# report_generation. Plus a structured extractor for the metric table.
# ---------------------------------------------------------------------------


def _iter_report_files(
    root: Path, ticker: str, model: str | None
) -> list[Path]:
    search_dirs: list[Path] = []
    if root.is_dir():
        has_md = any(
            f.name.endswith('.md') and f'report_generation_{ticker}_' in f.name
            for f in root.iterdir() if f.is_file()
        )
        if has_md:
            search_dirs.append(root)
        else:
            for sub in sorted(root.iterdir()):
                if not sub.is_dir():
                    continue
                if not sub.name.startswith(f'report_generation_{ticker}_'):
                    continue
                if model and not sub.name.endswith('_' + model):
                    continue
                search_dirs.append(sub)
    report_files: list[Path] = []
    for search_dir in search_dirs:
        for fpath in sorted(search_dir.iterdir()):
            if (
                fpath.is_file()
                and fpath.name.endswith('.md')
                and fpath.name.startswith(f'report_generation_{ticker}_')
            ):
                report_files.append(fpath)
    return report_files


@mcp.tool(
    description=(
        "List generated report markdown files for a ticker under the configured reports root. "
        "Optionally filter by model."
    )
)
def list_reports(
    ticker: Annotated[str, Field(description="Ticker symbol, e.g. TSLA")],
    model: Annotated[Optional[str], Field(description="Optional model filter")] = None,
) -> list[dict]:
    root = _get_reports_root()
    files = _iter_report_files(root, ticker, model)
    results: list[dict] = []
    pattern = re.compile(r'^report_generation_([A-Z]+)_(\d{8})_(.+)\.md$')
    for fpath in files:
        match = pattern.match(fpath.name)
        if not match:
            continue
        file_ticker, date_str, file_model = match.groups()
        results.append({
            "filename": fpath.name,
            "relative_path": str(fpath.relative_to(root)),
            "ticker": file_ticker,
            "report_date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
            "model": file_model,
        })
    results.sort(key=lambda x: x["report_date"])
    return results


@mcp.tool(
    description="Read one generated report markdown file by its relative path returned from list_reports."
)
def get_report_content(
    relative_path: Annotated[str, Field(description="Relative path returned by list_reports")],
) -> dict:
    root = _get_reports_root()
    path = (root / relative_path).resolve()
    if root not in path.parents and path != root:
        raise ValueError("relative_path escapes reports root")
    if not path.is_file():
        raise FileNotFoundError(f"Report file not found: {path}")
    return {
        "relative_path": relative_path,
        "filename": path.name,
        "content": path.read_text(encoding="utf-8"),
    }


# Mapping from human-readable labels in the generated Markdown table to the
# canonical keys returned by report_generation_mcp.get_weekly_metrics.
# This intentionally matches the latest report_generation SKILL table: 16
# required alpha/momentum/beta metrics plus four context rows used in prose.
_METRIC_LABEL_TO_KEY: dict[str, str] = {
    "week open": "week_open",
    "week close": "week_close",
    "weekly return": "weekly_return_pct",
    "4-week return": "return_4week_pct",
    "20-day ma": "ma_20day",
    "price vs 20-day ma": "price_vs_ma20",
    "weekly volatility": "weekly_volatility",
    "distance from 52-week high": "dist_from_52w_high_pct",
    "20-day support": "support_20d",
    "20-day resistance": "resistance_20d",
    "short-term momentum": "momentum_short",
    "macd signal": "macd_signal",
    "rsi (14)": "rsi_14",
    "volume ratio (vs 20d avg)": "volume_ratio",
    "chaikin money flow (20d)": "cmf_20day",
    "sector basket return (1w)": "sector_basket_return_1w_pct",
    "relative return (1w)": "relative_return_1w_pct",
    "relative return (4w)": "relative_return_4w_pct",
    "correlation (60d)": "correlation_60d",
    "beta (60d)": "beta_60d",
}

# The 16 metrics required by the generation skill. Context rows are parsed too,
# but missing context rows should not fail quantitative alignment.
_REQUIRED_METRIC_KEYS: tuple[str, ...] = (
    "week_open", "week_close", "weekly_return_pct", "return_4week_pct",
    "ma_20day", "price_vs_ma20", "weekly_volatility",
    "dist_from_52w_high_pct", "momentum_short", "macd_signal", "rsi_14",
    "sector_basket_return_1w_pct", "relative_return_1w_pct",
    "relative_return_4w_pct", "correlation_60d", "beta_60d",
)

# Categorical metrics keep their string form (lowercased).
_CATEGORICAL_METRIC_KEYS = {"price_vs_ma20", "momentum_short", "macd_signal"}

_RATING_PATTERN = re.compile(r"\b(STRONG_BUY|STRONG_SELL|BUY|SELL|HOLD)\b")


def _parse_metric_value(key: str, raw: str) -> object:
    """Parse a value cell from the report's metric table into a typed value."""
    raw = raw.strip()
    if key in _CATEGORICAL_METRIC_KEYS:
        return raw.lower() if raw else None
    raw_lower = raw.lower()
    if (
        not raw
        or raw == "-"
        or raw_lower == "n/a"
        or raw_lower.startswith("n/a ")
        or raw_lower.startswith("n/a(")
        or raw_lower.startswith("n/a (")
        or "no peers" in raw_lower
    ):
        return None
    # rsi_14 sometimes appears as "50.8 (neutral)" — keep just the number,
    # the rsi_class is parsed separately if needed.
    if key == "rsi_14":
        m = re.match(r"^([\-+0-9.]+)", raw)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    # Strip currency, percent, comma, "x", trailing junk.
    cleaned = raw.replace("$", "").replace(",", "").replace("%", "").replace("×", "").replace("x", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return raw


@mcp.tool(
    description=(
        "Parse the structured fields out of a generated WEEKLY report markdown "
        "file: rating + the latest weekly metrics from the 'Weekly Price Performance' "
        "table. Returns {relative_path, filename, report_date, rating, "
        "rating_numeric, metrics, parse_warnings}.\n\n"
        "metric keys are aligned with report_generation_mcp.get_weekly_metrics, "
        "including alpha, momentum/volume, support/resistance, and beta rows.\n\n"
        "Use this INSTEAD of having the agent regex-parse markdown by hand."
    )
)
def get_report_metrics(
    relative_path: Annotated[str, Field(description="Relative path returned by list_reports")],
) -> dict:
    root = _get_reports_root()
    path = (root / relative_path).resolve()
    if root not in path.parents and path != root:
        raise ValueError("relative_path escapes reports root")
    if not path.is_file():
        raise FileNotFoundError(f"Report file not found: {path}")

    text = path.read_text(encoding="utf-8")
    warnings: list[str] = []

    # --- rating -------------------------------------------------------------
    rating = None
    for line in text.splitlines():
        if "rating" in line.lower() and ":" in line:
            tail = line.split(":", 1)[1]
            m = _RATING_PATTERN.search(tail)
            if m:
                rating = m.group(1)
                break
    if rating is None:
        m = _RATING_PATTERN.search(text)
        if m:
            rating = m.group(1)
            warnings.append("rating extracted from body (no explicit Rating: line)")
    rating_numeric = RATING_TO_NUMERIC.get(rating) if rating else None
    if rating and rating_numeric is None:
        warnings.append(f"unrecognized rating token: {rating!r}")

    # --- metrics table ------------------------------------------------------
    metrics: dict[str, object] = {}
    table_row_re = re.compile(r"^\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")
    for line in text.splitlines():
        m = table_row_re.match(line)
        if not m:
            continue
        label, raw_value = m.group(1), m.group(2)
        label_norm = label.strip().lower()
        if label_norm in ("metric", "value") or label_norm.startswith("---"):
            continue
        key = _METRIC_LABEL_TO_KEY.get(label_norm)
        if key is None:
            continue
        metrics[key] = _parse_metric_value(key, raw_value)

    missing_required = [k for k in _REQUIRED_METRIC_KEYS if k not in metrics]
    missing_context = [
        k for k in _METRIC_LABEL_TO_KEY.values()
        if k not in metrics and k not in _REQUIRED_METRIC_KEYS
    ]
    if missing_required:
        warnings.append(f"missing required metrics in table: {missing_required}")
    if missing_context:
        warnings.append(f"missing context metrics in table: {missing_context}")

    # --- report_date from filename -----------------------------------------
    report_date = None
    fn_match = re.match(
        r"^(.+)_report_generation_([A-Z]+)_(\d{8})_(.+)\.md$", path.name
    )
    if fn_match:
        ds = fn_match.group(3)
        report_date = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"

    return {
        "relative_path": relative_path,
        "filename": path.name,
        "report_date": report_date,
        "rating": rating,
        "rating_numeric": rating_numeric,
        "metrics": metrics,
        "parse_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Generation mirrors — recompute the SAME ground truth the generator
# computed at report time, using identical logic to report_generation_mcp.
# Critical for fair quantitative_alignment scoring.
# ---------------------------------------------------------------------------


def _classify_macd(macd_line: float, signal_line: float, hist: float, prev_hist: float) -> str:
    """Mirrors report_generation_mcp._classify_macd."""
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
        "Recompute the canonical weekly metrics for (symbol, report_date) "
        "using the SAME logic as report_generation_mcp.get_weekly_metrics. "
        "This is the GROUND TRUTH used to score quantitative_alignment.\n\n"
        "Returned keys match get_weekly_metrics, including the 16 required "
        "alpha/momentum/beta metrics plus support/resistance, CMF, volume, "
        "raw MACD values, RSI class, week_trading_days, and benchmark_basket.\n\n"
        "If report_date is not a trading day, raises ValueError; the "
        "evaluator should treat that report as not scorable on quantitative "
        "alignment."
    )
)
def verify_weekly_metrics(
    symbol: Annotated[str, Field(description="Stock symbol")],
    report_date: Annotated[str, Field(description="The report's week-ending date YYYY-MM-DD")],
) -> dict:
    sql = (
        "SELECT CAST(date AS VARCHAR) AS date, open, high, low, close, adj_close, volume "
        "FROM prices WHERE symbol = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 260"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, report_date]).fetchall()

    if not rows or rows[0][0] != report_date:
        raise ValueError(
            f"No price row for {symbol} on {report_date}; not a trading day."
        )

    df = pd.DataFrame(
        rows,
        columns=["date", "open", "high", "low", "close", "adj_close", "volume"],
    ).sort_values("date").reset_index(drop=True)

    end_dt = pd.to_datetime(report_date)
    # Mirror of report_generation_mcp: anchor the window to the Monday of
    # TARGET_DATE's ISO week so non-Friday TARGET_DATE doesn't pull in prior
    # week's Friday. Must stay in sync with report_generation_mcp.
    week_start_dt = end_dt - pd.Timedelta(days=end_dt.weekday())
    df_dates = pd.to_datetime(df["date"])
    this_week_mask = (df_dates >= week_start_dt) & (df_dates <= end_dt)
    this_week = df[this_week_mask].reset_index(drop=True)

    if len(this_week) == 0:
        raise ValueError(
            f"No trading days found in the week ending {report_date} for {symbol}."
        )

    # Price block
    week_open = float(this_week.iloc[0]["open"])
    week_close = float(this_week.iloc[-1]["adj_close"])
    week_high = float(this_week["high"].max())
    week_low = float(this_week["low"].min())

    # Returns block
    pre_week = df[~this_week_mask & (df_dates < week_start_dt)]
    if len(pre_week) > 0:
        prev_week_close = float(pre_week.iloc[-1]["adj_close"])
        weekly_return_pct = round(
            (week_close - prev_week_close) / prev_week_close * 100, 2
        )
    else:
        weekly_return_pct = None

    if len(df) >= 21:
        past_20_close = float(df.iloc[-21]["adj_close"])
        return_4week_pct = round(
            (week_close - past_20_close) / past_20_close * 100, 2
        )
    else:
        return_4week_pct = None

    # MA block
    ma_5day = round(float(df["adj_close"].tail(5).mean()), 4) if len(df) >= 5 else None
    ma_20day = round(float(df["adj_close"].tail(20).mean()), 4) if len(df) >= 20 else None
    ma_60day = round(float(df["adj_close"].tail(60).mean()), 4) if len(df) >= 60 else None
    price_vs_ma20 = (
        ("above" if week_close > ma_20day else "below") if ma_20day is not None else None
    )

    # Support / resistance and fund-flow context
    last_20 = df.tail(20)
    support_20d = round(float(last_20["low"].min()), 4) if len(last_20) >= 1 else None
    resistance_20d = round(float(last_20["high"].max()), 4) if len(last_20) >= 1 else None

    cmf_20day = None
    if len(last_20) >= 1:
        hl_range = (last_20["high"] - last_20["low"]).replace(0, float("nan"))
        mfm = ((last_20["close"] - last_20["low"]) - (last_20["high"] - last_20["close"])) / hl_range
        mfv = mfm * last_20["volume"]
        total_vol = last_20["volume"].sum()
        if total_vol > 0:
            cmf_20day = round(float(mfv.sum() / total_vol), 4)

    # Risk block
    this_week_returns = this_week["adj_close"].pct_change().dropna()
    if len(this_week_returns) >= 2:
        weekly_volatility = round(
            float(this_week_returns.std() * (5 ** 0.5) * 100), 2
        )
    else:
        weekly_volatility = None

    # Momentum block
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
            macd_signal = _classify_macd(
                macd_line, macd_sig_line, macd_hist, prev_hist
            )
            macd_values = {
                "line": round(macd_line, 4),
                "signal": round(macd_sig_line, 4),
                "hist": round(macd_hist, 4),
            }

    rsi_14 = None
    rsi_class = None
    if len(df) >= 15:
        rsi_series = _ta.rsi(df["adj_close"].astype(float), length=14).dropna()
        if len(rsi_series) > 0:
            rsi_14 = round(float(rsi_series.iloc[-1]), 2)
            rsi_class = _classify_rsi(rsi_14)

    # Volume block
    volume_ratio = None
    this_week_avg_vol = float(this_week["volume"].mean())
    pre_window = pre_week.tail(20) if len(pre_week) >= 20 else pre_week
    if len(pre_window) > 0:
        pre_avg_vol = float(pre_window["volume"].mean())
        if pre_avg_vol > 0:
            volume_ratio = round(this_week_avg_vol / pre_avg_vol, 2)

    # Position block
    last_252 = df.tail(252)
    high_52w = float(last_252["high"].max())
    low_52w = float(last_252["low"].min())
    dist_from_52w_high_pct = round((week_close - high_52w) / high_52w * 100, 2)
    dist_from_52w_low_pct = round((week_close - low_52w) / low_52w * 100, 2)

    # Beta / relative-performance block
    peers = PEER_MAP.get(symbol, {}).get("peers", [])
    beta_block = _compute_beta_block(
        symbol=symbol,
        peers=peers,
        target_date=report_date,
        week_start_dt=week_start_dt,
        end_dt=end_dt,
    )

    return {
        "week_open": week_open,
        "week_close": week_close,
        "week_high": week_high,
        "week_low": week_low,
        "weekly_return_pct": weekly_return_pct,
        "return_4week_pct": return_4week_pct,
        "ma_5day": ma_5day,
        "ma_20day": ma_20day,
        "ma_60day": ma_60day,
        "price_vs_ma20": price_vs_ma20,
        "weekly_volatility": weekly_volatility,
        "momentum_short": momentum_short,
        "macd_signal": macd_signal,
        "rsi_14": rsi_14,
        "volume_ratio": volume_ratio,
        "cmf_20day": cmf_20day,
        "support_20d": support_20d,
        "resistance_20d": resistance_20d,
        "dist_from_52w_high_pct": dist_from_52w_high_pct,
        "dist_from_52w_low_pct": dist_from_52w_low_pct,
        "sector_basket_return_1w_pct": beta_block["sector_basket_return_1w_pct"],
        "relative_return_1w_pct": beta_block["relative_return_1w_pct"],
        "relative_return_4w_pct": beta_block["relative_return_4w_pct"],
        "correlation_60d": beta_block["correlation_60d"],
        "beta_60d": beta_block["beta_60d"],
        "benchmark_basket": beta_block["benchmark_basket"],
        "macd_values": macd_values,
        "rsi_class": rsi_class,
        "week_trading_days": int(len(this_week)),
    }


# ---------------------------------------------------------------------------
# Evaluation-specific helpers — forward returns, leakage checks, evidence
# search. These have no analog in the generation server.
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Compute forward returns for (symbol, report_date) over multiple "
        "horizons (in trading days). For each horizon h, returns the percent "
        "return from report_date's adj_close to the adj_close h trading days "
        "later. Returns {symbol, report_date, base_close, horizons: {'1d': "
        "{date, close, return_pct}, ...}, available_horizons}.\n\n"
        "If a horizon extends past the last available trading day in the DB, "
        "that horizon's value is null. This is the primary signal for "
        "backtesting whether the report's rating played out."
    )
)
def get_forward_returns(
    symbol: Annotated[str, Field(description="Stock symbol")],
    report_date: Annotated[str, Field(description="The report's week-ending date YYYY-MM-DD")],
    horizons: Annotated[
        list[int],
        Field(description="List of forward horizons in trading days, e.g. [1, 5, 20]"),
    ] = [1, 5, 20],
) -> dict:
    if not horizons:
        raise ValueError("horizons must not be empty")
    max_h = max(horizons)
    sql = (
        "SELECT CAST(date AS VARCHAR), adj_close "
        "FROM prices WHERE symbol = ? AND date >= ? "
        "ORDER BY date ASC LIMIT ?"
    )
    with _connect() as conn:
        rows = conn.execute(sql, [symbol, report_date, max_h + 1]).fetchall()

    if not rows or rows[0][0] != report_date:
        raise ValueError(
            f"No price row for {symbol} on {report_date}; not a trading day."
        )

    base_close = float(rows[0][1])
    horizons_out: dict[str, Optional[dict]] = {}
    available: list[str] = []
    for h in horizons:
        key = f"{h}d"
        if h < len(rows):
            target_date_str, target_close = rows[h]
            ret = (float(target_close) - base_close) / base_close * 100
            horizons_out[key] = {
                "date": target_date_str,
                "close": float(target_close),
                "return_pct": round(ret, 2),
            }
            available.append(key)
        else:
            horizons_out[key] = None

    return {
        "symbol": symbol,
        "report_date": report_date,
        "base_close": base_close,
        "horizons": horizons_out,
        "available_horizons": available,
    }


@mcp.tool(
    description=(
        "Check whether the news article ids the agent cited in a report were "
        "all dated <= report_date. Returns one entry per id with "
        "{id, found, news_date, days_after_report, is_leak}. is_leak is true "
        "when news_date > report_date. Use this for the 'no future leakage' "
        "check in evidence_fidelity scoring."
    )
)
def check_news_leakage(
    symbol: Annotated[str, Field(description="Stock symbol")],
    news_ids: Annotated[list[int], Field(description="News ids cited or referenced in the report")],
    report_date: Annotated[str, Field(description="The report's week-ending date YYYY-MM-DD")],
) -> list[dict]:
    if not news_ids:
        return []
    placeholders = ",".join(["?"] * len(news_ids))
    sql = (
        f"SELECT id, CAST(DATE(date) AS VARCHAR) AS date "
        f"FROM news WHERE symbol = ? AND id IN ({placeholders})"
    )
    params: list = [symbol, *news_ids]
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    found_map = {int(r[0]): r[1] for r in rows}
    rd = date.fromisoformat(report_date)
    out = []
    for nid in news_ids:
        nid_int = int(nid)
        if nid_int not in found_map:
            out.append({
                "id": nid_int, "found": False,
                "news_date": None, "days_after_report": None,
                "is_leak": False,
            })
            continue
        nd_str = found_map[nid_int]
        nd = date.fromisoformat(nd_str)
        delta = (nd - rd).days
        out.append({
            "id": nid_int,
            "found": True,
            "news_date": nd_str,
            "days_after_report": delta,
            "is_leak": delta > 0,
        })
    return out


@mcp.tool(
    description=(
        "Search news highlights for a symbol in a window for keyword matches. "
        "Useful for evidence_fidelity: did the report mention an event that "
        "actually appeared in the news? Returns matching "
        "{id, date, highlights_chars, highlights_preview} rows where "
        "`highlights` contains any case-insensitive keyword. "
        "`highlights_preview` is the first `preview_chars` characters of the "
        "body (default 600); call `get_news_by_id` for the full text."
    )
)
def search_news(
    symbol: Annotated[str, Field(description="Stock symbol")],
    keywords: Annotated[list[str], Field(description="One or more keywords to match against the news highlights body")],
    date_start: Annotated[str, Field(description="Inclusive start date YYYY-MM-DD")],
    date_end: Annotated[str, Field(description="Inclusive end date YYYY-MM-DD")],
    limit: Annotated[int, Field(description="Max rows to return", ge=1)] = 50,
    preview_chars: Annotated[
        int,
        Field(
            description="Number of leading characters of `highlights` to return as preview (default 600, max 2000).",
            ge=0,
            le=2000,
        ),
    ] = 600,
) -> list[dict]:
    if not keywords:
        return []
    where_clauses = " OR ".join(["LOWER(highlights) LIKE ?"] * len(keywords))
    sql = (
        f"SELECT id, CAST(DATE(date) AS VARCHAR), "
        f"LENGTH(highlights) AS highlights_chars, "
        f"SUBSTRING(highlights, 1, ?) AS preview "
        f"FROM news "
        f"WHERE symbol = ? AND DATE(date) >= ? AND DATE(date) <= ? "
        f"AND ({where_clauses}) "
        f"ORDER BY date ASC LIMIT ?"
    )
    params: list = [int(preview_chars), symbol, date_start, date_end]
    for kw in keywords:
        params.append(f"%{kw.lower()}%")
    params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0], "date": r[1],
            "highlights_chars": int(r[2] or 0),
            "highlights_preview": r[3] or "",
        }
        for r in rows
    ]


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="report_evaluation_mcp MCP server")
    parser.add_argument(
        "--db-path", default=None,
        help=f"Path to DuckDB; env fallback ${REPORT_EVALUATION_DB_PATH_ENV}.",
    )
    parser.add_argument(
        "--reports-root", default=None,
        help=f"Path to report_generation outputs; env fallback ${REPORT_EVALUATION_REPORTS_ROOT_ENV}.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.db_path:
        DB_PATH = str(Path(args.db_path).expanduser().resolve())
    elif os.environ.get(REPORT_EVALUATION_DB_PATH_ENV):
        DB_PATH = os.environ[REPORT_EVALUATION_DB_PATH_ENV]
    elif os.environ.get(LEGACY_DB_PATH_ENV):
        DB_PATH = os.environ[LEGACY_DB_PATH_ENV]
    else:
        print(
            "report_evaluation_mcp: --db-path is required "
            f"(or set {REPORT_EVALUATION_DB_PATH_ENV}; legacy: {LEGACY_DB_PATH_ENV}).",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.reports_root:
        REPORTS_ROOT = str(Path(args.reports_root).expanduser().resolve())
    elif os.environ.get(REPORT_EVALUATION_REPORTS_ROOT_ENV):
        REPORTS_ROOT = os.environ[REPORT_EVALUATION_REPORTS_ROOT_ENV]
    else:
        print(
            "report_evaluation_mcp: --reports-root is required "
            f"(or set {REPORT_EVALUATION_REPORTS_ROOT_ENV}).",
            file=sys.stderr,
        )
        sys.exit(2)

    mcp.run()