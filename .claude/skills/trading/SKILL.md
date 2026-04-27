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
   If omitted, call `is_trading_day(SYMBOL, today_or_any_recent_date)` — the
   `latest_date_in_db` field in the response is the real cutoff to use as
   `TARGET_DATE`.

Typical user phrasings:
- `trade AAPL on 2025-03-05`
- `make trading decision for TSLA 2025-04-10`
- `trade NVDA` (no date → use latest in DuckDB)

---

## Data access — DuckDB via MCP

Tools on the `trading_mcp` server. The list/get pair pattern keeps individual
tool results small and lets the agent fetch detail on demand — bulk fetches
of full news highlights or filing bodies can exceed the model's context limit.

| Tool | Purpose |
|---|---|
| `get_prices(symbol, date_start, date_end)` | Rows `{symbol, date, open, high, low, close, adj_close, volume}` in the range. `adj_close` is the canonical trading price. |
| `is_trading_day(symbol, target_date)` | Returns `{is_trading_day, reason, prev_trading_day, prev_trading_day_adj_close, latest_date_in_db, should_upsert}`. `reason ∈ {'trading_day','weekend','holiday','not_loaded'}`. Use this **first** every day — it replaces weekday checks and missing-row checks, and exposes the latest loaded date. |
| `list_news(symbol, date_start, date_end, preview_chars?=300)` | Compact news metadata: `{symbol, date, id, highlights_chars, highlights_preview}` — preview is the first `preview_chars` chars of the body (no full highlights). Use this first to scan the lead of each day's coverage. |
| `get_news_by_id(symbol, id)` | Full article for one id: `{symbol, date, id, highlights}`. Call after `list_news` for the days whose preview looks relevant. |
| `list_filings(symbol, date_start, date_end, document_type?)` | Compact filings metadata: `{symbol, date, document_type, mda_chars, risk_chars}` — no content. Use to decide if/which section is worth reading. |
| `get_filing_section(symbol, date, document_type, section, offset=0, limit=None)` | Fetch one section (`'mda'` or `'risk'`) of a specific filing. Omit `limit` for the whole section; use `offset/limit` to paginate long sections. Returns `{content, total_chars, offset, returned_chars, has_more, …}`. |
| `get_indicator(symbol, date_start, date_end, indicator, length?)` | Computes a technical indicator. `indicator` ∈ {`ma`, `rsi`, `bbands`, `macd`}. Optional — use only if indicators help your decision. |

### Writing the result — `upsert_decision.py` (CLI, not MCP)

Use the standalone script `.claude/skills/trading/scripts/upsert_decision.py`
via the Bash tool to write each day's record. It owns all the file-I/O logic
(load-or-create, sanitize filename, upsert by date, sort, recompute
`start_date`/`end_date`, write JSON) so you don't have to write inline Python.
See the "Output — incremental upsert" section below for the full call.

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

- For every data tool (`get_prices` / `list_news` / `get_news_by_id` /
  `list_filings` / `get_filing_section` / `get_indicator`): any `date_end` /
  filing `date` / news date must be `<= TARGET_DATE`.
- `date_start` can be as far back as you want — historical context is always safe.

This keeps the decision valid under any data population policy.

### Typical call sequence on one day

1. **`is_trading_day(SYMBOL, TARGET_DATE)`** — always the first call.
   - `reason == "weekend"` or `"holiday"`: set `action = "HOLD"`,
     `price_today = prev_trading_day_adj_close`, skip straight to step 5.
   - `reason == "not_loaded"` (date later than `latest_date_in_db`): **stop
     and report to the user; do not run the upsert script.**
   - `reason == "trading_day"`: continue.

2. `get_prices(SYMBOL, date_start, TARGET_DATE)` — pick the window that fits
   your read (e.g. last 5 / 30 / 60 trading days). `price_today` is the
   `adj_close` of the row where `date == TARGET_DATE`; use the rest for trend
   and volatility context.

3. `list_news(SYMBOL, date_start, TARGET_DATE)` — pick a window that fits
   your need (e.g. the last 1, 3, or 7 days; one row per day). Scan each
   day's `highlights_preview` and call `get_news_by_id(SYMBOL, id)` for the
   days worth reading in full. Bump `preview_chars` if 300 isn't enough to
   judge.

4. (Optional) If news or price action suggests a fundamentals check:
   `list_filings(SYMBOL, date_start, TARGET_DATE)` with a window you choose
   (typically last 6 months or 1 year — older filings are stale and 1 year
   already covers a 10-K plus three 10-Qs). Look at `mda_chars` /
   `risk_chars` to decide whether the section is worth reading, then
   `get_filing_section(..., section='mda' | 'risk')`. For very long sections,
   paginate with `offset`/`limit` and stop when you have enough.

   (Optional) `get_indicator(SYMBOL, date_start, TARGET_DATE, 'rsi' | 'macd'
   | 'ma' | 'bbands')` with a window you choose (e.g. last 30 / 60 / 120
   trading days; longer windows give MACD/EMA more time to converge) if a
   technical signal would confirm / contradict your read.

5. **Run `.claude/skills/trading/scripts/upsert_decision.py` via Bash** to
   record the decision. The script owns load-or-create / sort /
   recompute-bounds / write. Don't write JSON yourself. Example:

   ```bash
   python3 .claude/skills/trading/scripts/upsert_decision.py \
       --symbol SYMBOL --target-date TARGET_DATE \
       --price PRICE_TODAY --action <BUY|SELL|HOLD> \
       --model <your model id> \
       --output-root <whatever the caller specified, e.g. /io/slot1>
   ```

   On success it prints one JSON line with `{path, action_recorded,
   date_recorded, total_records, start_date, end_date}`.

Compute date offsets with the bundled helper — one call covers every offset
you need for the day:

```bash
python3 .claude/skills/trading/scripts/date_offset.py TARGET_DATE 7 30 60 365
```

Prints one `<days>\t<YYYY-MM-DD>` line per offset, in argument order. Do not
write inline Python via Bash heredoc to recompute this each invocation.

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

**Detection: one call to `is_trading_day(SYMBOL, TARGET_DATE)`.** The `reason`
field gives you everything you need — skip all further data fetching if it's
not a trading day:

- `reason == "weekend"` → Saturday or Sunday → force `HOLD`.
- `reason == "holiday"` → market holiday (Presidents' Day, Good Friday, etc.) →
  force `HOLD`.
- `reason == "trading_day"` → proceed normally.
- `reason == "not_loaded"` (date later than `latest_date_in_db`) → the date is
  simply not yet in DB. **Stop and report to the user; do not run the upsert
  script.**

When forced to `HOLD` on a non-trading day, use the `prev_trading_day_adj_close`
field returned by `is_trading_day` as `price_today`, and briefly note to the
user that the date is a non-trading day.

---

## Output — incremental upsert

**Run the `upsert_decision.py` script via the Bash tool** — do NOT write
inline Python for the write step, and do NOT generate the full JSON yourself.
The script lives at `.claude/skills/trading/scripts/upsert_decision.py` and
owns everything: sanitizes filename, loads-or-creates the JSON, upserts the
record by `target_date`, sorts, recomputes `start_date`/`end_date`, writes.

### How to call it

```bash
python3 .claude/skills/trading/scripts/upsert_decision.py \
    --symbol TSLA \
    --target-date 2025-03-03 \
    --price 284.65 \
    --action BUY \
    --model claude-sonnet-4-6 \
    --output-root /io/slot1   # whatever the caller specified
```

| Flag | Value |
|---|---|
| `--symbol` | `SYMBOL`, e.g. `TSLA` |
| `--target-date` | `TARGET_DATE` as `YYYY-MM-DD` |
| `--price` | `adj_close` from the `get_prices` row for `TARGET_DATE`, or `prev_trading_day_adj_close` on a forced HOLD |
| `--action` | Exactly `BUY`, `SELL`, or `HOLD` |
| `--model` | Your actual model identifier — the only run-differentiator in the filename |
| `--output-root` | **Pass the value the caller specified in the invocation** (e.g. `/io/slot1`). Falls back to `results/trading` (relative to cwd) only if no value was given — that default is rarely writable inside a sandbox, so omitting it usually causes a `PermissionError`. |

### What it writes

Target file path (derived by the script, don't build it yourself):

```
results/trading/trading_{SYMBOL}_{model}.json
```

Sanitization rule (the script applies it for you): any character that is not
alphanumeric / `-` / `_` becomes `_`, and `model` is lowercased. Examples:
`trading_TSLA_claude-sonnet-4-6.json`, `trading_AAPL_gpt-5.json`.

Calling the script again with the same `--target-date` **overwrites** that
date's record (lets the caller re-run one day).

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
- Do **not** write inline Python via Bash heredoc (`python - <<'PY' ... PY`)
  to produce the result JSON — use the `upsert_decision.py` CLI script.
- Do **not** write inline Python to compute `TARGET_DATE - Nd` offsets —
  use `scripts/date_offset.py` (one call covers every offset for the day).
- Do **not** compute weekday / prior-trading-day yourself — call
  `is_trading_day` and use the `prev_trading_day_adj_close` it returns.

---

## Implementation approach

1. Resolve `TARGET_DATE`: if the user provided one, use it. Otherwise call
   `is_trading_day(SYMBOL, today_or_any_recent_date)` and use the
   `latest_date_in_db` field from the response as `TARGET_DATE`.
2. **`is_trading_day(SYMBOL, TARGET_DATE)`** — branch on `reason`:
   - `"weekend"` or `"holiday"`: set `action = "HOLD"`,
     `price_today = prev_trading_day_adj_close`, skip to step 6.
   - `"not_loaded"`: **stop and report to the user**, do not run the upsert
     script.
   - `"trading_day"`: continue.
3. `get_prices(SYMBOL, date_start, TARGET_DATE)` with a window you choose
   (e.g. last 5 / 30 / 60 trading days). Extract `price_today` = `adj_close`
   of the row where `date == TARGET_DATE`.
4. `list_news(SYMBOL, date_start, TARGET_DATE)` with a window you choose
   (last 1 / 3 / 7 days, one row per day) → scan `highlights_preview` →
   `get_news_by_id(SYMBOL, id)` for the days worth reading in full.
5. (Optional) If fundamentals matter, `list_filings(SYMBOL, date_start,
   TARGET_DATE)` with a window you choose (typically 6 months or 1 year) →
   `get_filing_section(..., section='mda' | 'risk')` for the section(s) worth
   reading. Paginate with `offset`/`limit` if the section is long and you
   want to stop partway through.
   (Optional) `get_indicator(SYMBOL, date_start, TARGET_DATE, 'rsi' | 'macd'
   | 'ma' | 'bbands')` with a window you choose (e.g. 30 / 60 / 120 trading
   days) if a technical signal helps.
6. Decide `action` based on the data you actually fetched.
7. Run `python3 .claude/skills/trading/scripts/upsert_decision.py` via the
   Bash tool with the 5 required flags. Don't write inline Python.

One record in, one record out. The caller decides when to mark `status` as
`completed` — you always leave it `in_progress`.
