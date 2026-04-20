---
name: trading
description: >
  Makes a single daily BUY/SELL/HOLD decision for one stock on a given target_date
  by querying an offline DuckDB via MCP tools. The skill is driven externally: each
  invocation handles exactly one (ticker, target_date) pair and upserts the result
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

You are making a **single-day** trading decision for one ticker on one target
date. You call MCP tools on the `trading_mcp` server to read prices, news,
filings, and technical indicators from an offline DuckDB, reason over what you
see, then upsert one record into an action-list JSON file.

Everything you know about the market comes from the MCP tools described below.

---

## Inputs

The user invocation specifies:

1. **`TICKER`** — one of the 10 supported tickers:
   `AAPL`, `ADBE`, `AMZN`, `BMRN`, `CRM`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`
2. **`TARGET_DATE`** — the trading day to decide on, `YYYY-MM-DD`. **Optional.**
   If omitted, call `get_latest_date(ticker=TICKER)` and use the returned date.

Typical user phrasings:
- `trade AAPL on 2025-03-05`
- `make trading decision for TSLA 2025-04-10`
- `trade NVDA` (no date → use latest in DuckDB)

---

## Data access — DuckDB via MCP

Five tools are available on the `trading_mcp` server:

| Tool | Purpose |
|---|---|
| `get_prices(ticker, date_start, date_end)` | Rows `{ticker, date, price, momentum}` in the range. Also used to discover which dates have data. |
| `get_news(ticker, date_start, date_end)` | Rows `{ticker, date, item_id, content}` — zero or more items per date. |
| `get_filings(ticker, date_start, date_end, form_type?)` | Rows `{ticker, filing_date, form_type, content}` whose `filing_date` falls in the range. `form_type` is `"10-K"`, `"10-Q"`, or omitted for both. |
| `get_indicator(ticker, date_start, date_end, indicator, length?)` | Computes a technical indicator from the prices table. `indicator` ∈ {`ma`, `rsi`, `bbands`, `macd`}. Returns per-date rows whose keys depend on the indicator (see below). Optional — use only if indicators help your decision. |
| `get_latest_date(ticker)` | Returns the latest trading date available in DuckDB for the ticker. Use only when `TARGET_DATE` is not supplied. |

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

1. `get_prices(TICKER, TARGET_DATE - 30d, TARGET_DATE)` — recent ~1 month of prices + momentum labels.
2. `get_news(TICKER, TARGET_DATE - 7d, TARGET_DATE)` — last week of news.
3. If the news or recent moves warrant fundamentals, `get_filings(TICKER, TARGET_DATE - 1y, TARGET_DATE)` — past-year filings.
4. Optionally `get_indicator(TICKER, TARGET_DATE - 5d, TARGET_DATE, indicator="your_indicator")` or similar when a technical signal would help confirm/contradict your read. Skip if the price action is obvious or news-driven.

Compute date offsets in Python (`datetime.date.fromisoformat(TARGET_DATE) - timedelta(days=N)`).

---

## Reasoning and decision

Produce one of: **BUY** (expect upward move), **SELL** (expect downward move),
**HOLD** (uncertain / no position).

Ground your decision in the data you actually fetched via MCP. The decision is
the only artifact saved — no rationale field is written to the output file.

### Non-trading-day rule (forced HOLD)

If `TARGET_DATE` is not an actual US-market trading day (weekend or market
holiday), the decision **must be `HOLD`**. The DuckDB forward-fills prices on
non-trading days using the prior trading day's close, so `get_prices` will still
return a row — the sanity check alone is not enough to rule out non-trading
days. You must explicitly test for this before reasoning.

Detection — apply **both** checks; if either fires, force `HOLD` and skip all
further data fetching / reasoning:

1. **Weekday check.** Compute `datetime.date.fromisoformat(TARGET_DATE).weekday()`.
   If it is `5` (Saturday) or `6` (Sunday) → non-trading day → `HOLD`.
2. **Forward-fill check** (catches market holidays like Presidents' Day, Good
   Friday, Thanksgiving, etc.). Query
   `get_prices(TICKER, TARGET_DATE - 4 days, TARGET_DATE)`. Take the most recent
   row strictly before `TARGET_DATE` in the returned list. If its `price`
   equals `TARGET_DATE`'s `price` exactly (float equality), treat `TARGET_DATE`
   as forward-filled → non-trading day → `HOLD`.

When forced to `HOLD`, still upsert the record using `price_today` from the
`TARGET_DATE` row (the forward-filled value), and briefly note to the user that
the date is a non-trading day.

---

## Output — incremental upsert

Write to:

```
results/trading/trading_{TICKER}_{agent_name}_{model}.json
```

where `agent_name` is your name (e.g. `claude-code`, `codex`). Sanitize
`TICKER` and `model` for filename use: replace any character that is not
alphanumeric, `-`, or `_` with `_`; lowercase the model name. Examples:
`trading_TSLA_claude-code_claude-sonnet-4-6.json`,
`trading_AAPL_codex_gpt-5-4.json`.

The file holds one document with a `recommendations` array. Each invocation
upserts exactly one record keyed by `date`. Use this inline Python via the Bash
tool — do **not** generate the full JSON yourself and pass it to Write:

```python
import json, os
from pathlib import Path

out_path = Path(f"results/trading/trading_{TICKER}_{agent_name}_{model}.json")
out_path.parent.mkdir(parents=True, exist_ok=True)

if out_path.exists():
    doc = json.loads(out_path.read_text())
else:
    doc = {"status": "in_progress", "recommendations": []}

rec_by_date = {r["date"]: r for r in doc.get("recommendations", [])}
rec_by_date[TARGET_DATE] = {
    "date": TARGET_DATE,
    "price": price_today,            # from get_prices row for TARGET_DATE
    "recommended_action": action,    # "BUY" | "SELL" | "HOLD"
}

recs = sorted(rec_by_date.values(), key=lambda r: r["date"])

doc = {
    "status": "in_progress",
    "symbol": TICKER,
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
| `price` | `price` value from `get_prices` row for `TARGET_DATE` |
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
   `get_latest_date(TICKER)`.
2. **Sanity-check that `TARGET_DATE` has data.** Call
   `get_prices(TICKER, TARGET_DATE - 4 days, TARGET_DATE)`. If it returns no
   row for `TARGET_DATE`, that date is not yet loaded into DuckDB — **stop and
   report to the user**, do not fabricate a decision. Do not upsert a record.
3. **Non-trading-day check (forced HOLD).** Using the rows returned in step 2:
   (a) if `TARGET_DATE`'s weekday is Saturday or Sunday, **or** (b) if the price
   on `TARGET_DATE` equals the price on the most recent prior row exactly, set
   `action = "HOLD"` and skip straight to step 7. Do not call `get_news`,
   `get_filings`, or `get_indicator` on non-trading days.
4. Call `get_prices` (wider range for trend), `get_news`, and (if warranted)
   `get_filings` — all with ranges ending at `TARGET_DATE`. Optionally call
   `get_indicator` for one or more of `ma` / `rsi` / `bbands` / `macd` when a
   technical signal would help confirm or contradict your read.
5. Extract `price_today` from the `get_prices` row where `date == TARGET_DATE`.
6. Decide `action`, weighing price trend, news, filings, and any indicators you
   fetched.
7. Run the inline Python upsert above via Bash.

One record in, one record out. The caller decides when to mark `status` as
`completed` — you always leave it `in_progress`.
