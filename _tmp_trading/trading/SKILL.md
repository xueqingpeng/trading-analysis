---
name: trading
description: >
  Makes a single daily BUY/SELL/HOLD decision for one stock on a given target_date
  by querying an offline DuckDB via MCP tools. The skill is driven externally: each
  invocation handles exactly one (symbol, target_date) pair and upserts the result
  into an action-list JSON file. Data access is via the `trading_mcp` server,
  which reads from an offline DuckDB. The same skill powers both backtest-style 
  replay (caller loops over historical dates) and live trading (caller passes 
  today's date, or omits it so the skill uses the latest date available in DuckDB).

  Use this skill whenever the user asks you to make a trading decision for a single
  stock on a specific date, run a live trading step, or append one record to a
  trading action list — phrased as "trade AAPL on 2025-03-05", "decide TSLA today",
  "run trading for MSFT 2025-04-10", or just "trade NVDA".
---

# Trading Skill

You are making a **single-day** trading decision for one symbol on one target
date. You call MCP tools on the `trading_mcp` server to read prices, news,
filings, and technical indicators from an offline DuckDB, reason over what you
see, then upsert one record into an action-list JSON file.

Everything you know about the market comes from the MCP tools described below.

---

## Inputs

The user invocation specifies:

1. **`SYMBOL`** — one of the 8 supported symbols:
   `AAPL`, `ADBE`, `AMZN`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`
2. **`TARGET_DATE`** — the trading day to decide on, `YYYY-MM-DD`. **Optional.**
   If omitted, call `get_latest_date(symbol=SYMBOL)` and use the returned date.

Typical user phrasings:
- `trade AAPL on 2025-03-05`
- `make trading decision for TSLA 2025-04-10`
- `trade NVDA` (no date → use latest in DuckDB)

---

## Data access — DuckDB via MCP

Five tools are available on the `trading_mcp` server:

| Tool | Purpose |
|---|---|
| `get_prices(symbol, date_start, date_end)` | Rows `{symbol, date, open, high, low, close, adj_close, volume}` in the range. `adj_close` is the canonical trading price. Also used to discover which dates have data. |
| `get_news(symbol, date_start, date_end)` | Rows `{symbol, date, id, title, highlights}` — zero or more items per date. |
| `get_filings(symbol, date_start, date_end, document_type?)` | Rows `{symbol, date, document_type, mda_content, risk_content}` whose `date` falls in the range. `document_type` is `"10-K"`, `"10-Q"`, or omitted for both. |
| `get_indicator(symbol, date_start, date_end, indicator, length?)` | Computes a technical indicator from the prices table. `indicator` ∈ {`ma`, `rsi`, `bbands`, `macd`}. Returns per-date rows whose keys depend on the indicator (see below). Optional — use only if indicators help your decision. |
| `get_latest_date(symbol)` | Returns the latest trading date available in DuckDB for the symbol. Use only when `TARGET_DATE` is not supplied. |

### `get_indicator` return shapes

| indicator | default length | row shape |
|---|---|---|
| `ma` | 20 | `{date, ma}` |
| `rsi` | 14 | `{date, rsi}` |
| `bbands` | 20 (stddev=2) | `{date, upper, middle, lower}` |
| `macd` | fixed (12/26/9) | `{date, macd, hist, signal}` |

You can override `length` for `ma`/`rsi`/`bbands` (e.g. `length=50` for a 50-day MA); `macd` ignores `length`. The tool auto-fetches warmup history before `date_start` internally.

### No-look-ahead discipline

The DuckDB may or may not contain data past `TARGET_DATE` (depends on whether
this is live or a historical replay — the skill doesn't know, and shouldn't
care). Either way, **your queries must not request data beyond `TARGET_DATE`**:

- For all three data tools (`get_prices` / `get_news` / `get_filings`):
  `date_end` must be `<= TARGET_DATE`.
- `date_start` can be as far back as you want — historical context is always safe.

This keeps the decision valid under any data population policy.

### Typical call sequence on one day

1. `get_prices(SYMBOL, TARGET_DATE - 30d, TARGET_DATE)` — recent ~1 month of OHLCV. Use `adj_close` for price comparisons and trend analysis; `high`/`low` for volatility.
2. `get_news(SYMBOL, TARGET_DATE - 7d, TARGET_DATE)` — last week of news. Each row has `title` and `highlights`; read `title` first to gauge relevance before processing `highlights`.
3. If the news or recent moves warrant fundamentals, `get_filings(SYMBOL, TARGET_DATE - 1y, TARGET_DATE)` — past-year filings. Each row has separate `mda_content` and `risk_content` sections.
4. Optionally call `get_indicator` for one or more signals when they would help confirm/contradict your read. Available: `ma`, `rsi`, `bbands`, `macd`. Skip if the price action is obvious or news-driven.

Compute date offsets in Python (`datetime.date.fromisoformat(TARGET_DATE) - timedelta(days=N)`).

---

## Reasoning and decision

Produce one of: **BUY** (expect upward move), **SELL** (expect downward move),
**HOLD** (uncertain / no position).

Ground your decision in the data you actually fetched via MCP. The decision is
the only artifact saved — no rationale field is written to the output file.

### Non-trading-day rule (forced HOLD)

If `TARGET_DATE` is not an actual US-market trading day (weekend or market
holiday), the decision **must be `HOLD`**. The DuckDB only stores rows for
actual trading days — weekends and market holidays have **no row at all**.

Detection — apply the following checks against the `get_prices` result from
step 2 of the implementation approach. If either fires, skip all further data
fetching / reasoning:

1. **Weekday check.** Compute `datetime.date.fromisoformat(TARGET_DATE).weekday()`.
   If it is `5` (Saturday) or `6` (Sunday) → non-trading day → `HOLD`.
2. **Missing-row check** (catches market holidays like Presidents' Day, Good
   Friday, Thanksgiving, etc.). If the `get_prices` return list has **no row**
   where `date == TARGET_DATE`:
   - If `TARGET_DATE <= get_latest_date(SYMBOL)` → market holiday → `HOLD`.
   - If `TARGET_DATE > get_latest_date(SYMBOL)` → the date is simply not
     loaded yet. **Stop and report to the user**, do not upsert a record.

When forced to `HOLD` on a non-trading day, the `TARGET_DATE` row does not
exist. Use `adj_close` from the **most recent prior trading day** in the
returned list as `price_today`, and briefly note to the user that the date is
a non-trading day.

---

## Output — incremental upsert

Write to:

```
results/trading/trading_{SYMBOL}_{agent_name}_{model}.json
```

where `agent_name` is your name (e.g. `claude-code`, `codex`). Sanitize
`SYMBOL` and `model` for filename use: replace any character that is not
alphanumeric, `-`, or `_` with `_`; lowercase the model name. Examples:
`trading_TSLA_claude-code_claude-sonnet-4-6.json`,
`trading_AAPL_codex_gpt-5.json`.

The file holds one document with a `recommendations` array. Each invocation
upserts exactly one record keyed by `date`. Use this inline Python via the Bash
tool — do **not** generate the full JSON yourself and pass it to Write:

```python
import json, os
from pathlib import Path

out_path = Path(f"results/trading/trading_{SYMBOL}_{agent_name}_{model}.json")
out_path.parent.mkdir(parents=True, exist_ok=True)

if out_path.exists():
    doc = json.loads(out_path.read_text())
else:
    doc = {"status": "in_progress", "recommendations": []}

rec_by_date = {r["date"]: r for r in doc.get("recommendations", [])}
rec_by_date[TARGET_DATE] = {
    "date": TARGET_DATE,
    "price": price_today,            # adj_close from get_prices row for TARGET_DATE
    "recommended_action": action,    # "BUY" | "SELL" | "HOLD"
}

recs = sorted(rec_by_date.values(), key=lambda r: r["date"])

doc = {
    "status": "in_progress",
    "symbol": SYMBOL,
    "agent": agent_name,
    "model": model,
    "start_date": recs[0]["date"],
    "end_date": recs[-1]["date"],
    "recommendations": recs,
}

out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
```

Same `TARGET_DATE` called again → **overwrites** the prior record (lets the
caller re-run one day).

### Output record schema

| Field | Rule |
|---|---|
| `date` | `TARGET_DATE`, `YYYY-MM-DD` |
| `price` | `adj_close` from `get_prices` row for `TARGET_DATE` |
| `recommended_action` | Exactly `"BUY"`, `"SELL"`, or `"HOLD"` |

---

## What NOT to do

- Do **not** read parquet files directly. Data must come from MCP tools.
- Do **not** query MCP with `date_end > TARGET_DATE`.
- Do **not** rewrite the action list file from scratch — always upsert.
- Do **not** produce decisions for multiple dates in one invocation.
- Do **not** save intermediate scripts, debug logs, or partial output files.

---

## Implementation approach

1. Resolve `TARGET_DATE`: if the user provided one, use it. Otherwise call
   `get_latest_date(SYMBOL)`.
2. **Fetch recent prices.** Call
   `get_prices(SYMBOL, TARGET_DATE - 7 days, TARGET_DATE)`. The 7-day window
   guarantees at least one prior trading day even across long weekends.
3. **Non-trading-day / missing-data branch.** Using the rows returned in step 2:
   - (a) If `TARGET_DATE`'s weekday is Saturday or Sunday → set
     `action = "HOLD"`, set `price_today` from the most recent prior row's
     `adj_close`, and skip straight to step 7.
   - (b) Else if there is **no row** where `date == TARGET_DATE`:
     - If `TARGET_DATE <= get_latest_date(SYMBOL)` → market holiday → set
       `action = "HOLD"`, set `price_today` from the most recent prior row's
       `adj_close`, and skip straight to step 7.
     - Else → the date is not yet loaded. **Stop and report to the user**, do
       not upsert a record.
   - Do not call `get_news`, `get_filings`, or `get_indicator` on non-trading
     days.
4. Call `get_prices` again (wider range for trend), `get_news`, and (if
   warranted) `get_filings` — all with ranges ending at `TARGET_DATE`.
   Optionally call `get_indicator` for one or more of `ma` / `rsi` / `bbands`
   / `macd` when a technical signal would help confirm or contradict your read.
5. Extract `price_today` from `adj_close` in the `get_prices` row where `date == TARGET_DATE`.
6. Decide `action`, weighing price trend, news, filings, and any indicators you
   fetched.
7. Run the inline Python upsert above via Bash.

One record in, one record out. The caller decides when to mark `status` as
`completed` — you always leave it `in_progress`.
