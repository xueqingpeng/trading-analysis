---
name: pair_trading
description: >
  Makes one daily pair-trading decision for a fixed ordered stock pair, or
  selects the fixed pair on the first target-window trading day, by querying
  offline data through the `pair_trading_mcp` server. Each invocation handles
  exactly one target date and upserts one recommendation into
  `results/pair_trading/`. The same skill can be driven by an external
  date-loop for the full 2025-03-01 through 2025-05-31 benchmark window.

  Use this skill whenever the user asks for a pair-trading decision, pair
  selection, or one daily pair-trading step.
---

# Pair Trading Skill

You are making a **single-day** pair-trading decision. On the first actual
trading day in the benchmark window, select one ordered pair from the configured
pool using only information visible by that date. On later days, trade the same
fixed pair and upsert exactly one result record.

The fixed benchmark window is:

```
2025-03-01 through 2025-05-31 inclusive
```

Everything you know about market data must come from MCP tools on the
`pair_trading_mcp` server. Do not read DuckDB or parquet files directly.

---

## Inputs

The user invocation may specify:

1. **`TARGET_DATE`** - the day to decide on, `YYYY-MM-DD`. Optional. If omitted,
   use the latest common trading date available for the selected pair, or use
   the first common pool trading date if no pair has been selected yet.
2. **`LEFT` / `RIGHT`** - optional fixed pair legs. If omitted and an existing
   pair-trading output already exists, reuse its fixed pair. If omitted and no
   existing pair is available, perform pair selection on the first actual common
   trading day in the benchmark window.

Typical user phrasings:
- `run pair trading for 2025-03-03`
- `pair trade META MSFT on 2025-04-10`
- `select a pair for the pair trading task`

---

## Data Access - MCP Tools

Tools on the `pair_trading_mcp` server. Prefer compact list/get pairs; bulk
returns of full news highlights or filing bodies can exceed the model context.

| Tool | Purpose |
|---|---|
| `get_stock_pool()` | Pool metadata: `{pool_name, symbols, backend, db_path, data_dir, file_pattern}`. |
| `get_common_trading_dates(date_start, date_end, symbols?)` | Sorted dates where every requested symbol has a price. Safe for calendar discovery only. |
| `is_pair_trading_day(left, right, target_date)` | Returns `{is_trading_day, reason, prices, latest_common_date, should_upsert}`. Use first for a fixed pair/day. |
| `get_prices(symbol, date_start, date_end)` | Compact price rows `{symbol, date, price, volume}`. Use for visible trends. |
| `get_pair_prices(left, right, target_date)` | Current pair prices on `target_date`, or `available=false`. |
| `[preferred] list_news(symbol, date_start, date_end)` | Compact news metadata `{symbol, date, id, title, url}`. No highlights. |
| `[preferred] get_news_by_id(symbol, id)` | Full selected news item `{symbol, date, id, title, url, highlights}`. |
| `[preferred] list_filings(symbol, date_start, date_end, document_type?)` | Compact filing metadata `{symbol, date, document_type, mda_chars, risk_chars}`. No section text. |
| `[preferred] get_filing_section(symbol, date, document_type, section, offset=0, limit=None)` | Fetch one filing section (`mda` or `risk`) with optional pagination. |
| `apply_pair_action(left, right, action, prices, current_position?)` | Deterministic position mechanics for `LONG_SHORT`, `SHORT_LONG`, `HOLD`, `CLOSE`. |

### Helper Scripts

Use helper scripts via Bash instead of writing inline Python:

```bash
python3 pair_trading/scripts/date_offset.py TARGET_DATE 7 30 60 365
```

```bash
python3 pair_trading/scripts/upsert_pair_decision.py \
    --left META --right MSFT --target-date 2025-03-03 \
    --left-price 182.45 --right-price 401.12 \
    --action LONG_SHORT \
    --trajectory "..." \
    --model <your model id>
```

Pass `--output-root=/path/to/output` if the caller supplies an output
directory. The upsert script owns filename sanitization, load-or-create, upsert
by date, sorting, recomputing date bounds, and writing JSON.

---

## No-Look-Ahead Discipline

The local dataset may contain rows after the day you are deciding. Your tool
calls must not request data beyond the current decision date:

- Pair selection: only use prices/news/filings with `date_end <= selection_date`.
- Daily trading: only use prices/news/filings with `date_end <= TARGET_DATE`.
- `get_common_trading_dates` may be called over the full benchmark window
  because it returns only the calendar of dates with prices, not market signals.
- Do not use future returns, future spread behavior, future news, or future
  filings to select the pair or decide an action.

---

## Pair Selection

If no fixed pair has been supplied or found in existing output:

1. Call `get_stock_pool()` and use its `symbols`.
2. Call `get_common_trading_dates("2025-03-01", "2025-05-31", symbols=pool)`.
   The first returned date is the `selection_date`.
3. For each candidate symbol, fetch compact visible context ending at
   `selection_date`:
   - `get_prices(symbol, selection_date - 30d, selection_date)`
   - `list_news(symbol, selection_date - 7d, selection_date)`
   - optionally `list_filings(symbol, selection_date - 1y, selection_date)`
4. Read full news/filing details only for items that look material from their
   titles or metadata.
5. Select exactly two distinct stocks and keep the fixed order `(left, right)`
   for the whole run.

A good pair is a relative-value candidate: one visibly stronger name versus a
weaker one, comparable companies with diverging catalysts, or a positive
catalyst on one side against negative or thinner signals on the other.

---

## Daily Pair Trading

For a fixed pair and `TARGET_DATE`:

1. Call `is_pair_trading_day(left, right, TARGET_DATE)` first.
   - `reason == "weekend"` or `"holiday"`: force `HOLD`, use returned prior
     prices, and skip further market-data fetching.
   - `reason == "not_loaded"`: stop and report to the user; do not upsert.
   - `reason == "missing_leg"`: stop and report which leg is unavailable; do
     not upsert unless the tool says `should_upsert=true`.
   - `reason == "trading_day"`: continue.
2. Fetch visible price context for both legs, normally from
   `TARGET_DATE - 30d` through `TARGET_DATE`.
3. Call `list_news` for both legs over the last week. Then call
   `get_news_by_id` only for relevant article ids.
4. Optionally call `list_filings` and then `get_filing_section` if fundamentals
   would materially affect the pair decision.
5. Choose exactly one daily action: `LONG_SHORT`, `SHORT_LONG`, or `HOLD`.
6. Optionally call `apply_pair_action` to maintain the deterministic position
   snapshot when deciding whether `HOLD` means keep existing exposure.
7. Run `upsert_pair_decision.py` with the final action and prices.

### Action Semantics

For fixed pair `("META", "MSFT")`:

- `LONG_SHORT` means long META, short MSFT.
- `SHORT_LONG` means short META, long MSFT.
- `HOLD` means keep existing exposure unchanged or initiate no exposure if
  there is no current position.
- `CLOSE` exists only in the mechanics tool; output recommendations must use
  exactly `LONG_SHORT`, `SHORT_LONG`, or `HOLD`.

Always state the long and short legs explicitly in `trajectory` when the action
opens or maintains directional exposure.

---

## Output

The upsert script writes:

```
results/pair_trading/pair_trading_{LEFT}_{RIGHT}_{model}.json
```

The file contains:

```json
{
  "status": "in_progress",
  "pair": "META, MSFT",
  "left": "META",
  "right": "MSFT",
  "start_date": "2025-03-03",
  "end_date": "2025-03-03",
  "model": "gpt-5",
  "recommendations": [
    {
      "pair": "META, MSFT",
      "date": "2025-03-03",
      "price": {
        "META": 182.45,
        "MSFT": 401.12
      },
      "recommended_action": "LONG_SHORT",
      "trajectory": "Pair remains META, MSFT because visible context favored META over MSFT. Today META showed stronger recent momentum while MSFT had weaker signals. Decision: LONG_SHORT - long META, short MSFT."
    }
  ]
}
```

The caller decides when to mark the run completed; daily invocations should
leave `status` as `in_progress` unless explicitly asked to finalize.

---

## What NOT To Do

- Do not read DuckDB or parquet files directly.
- Do not use `trading_mcp`; this skill uses `pair_trading_mcp`.
- Do not call data tools with `date_end > TARGET_DATE` for a decision.
- Do not pull all news highlights or filing bodies up front.
- Do not change the fixed pair after it has been selected.
- Do not compute statistics across the full future March-May window before
  trading.
- Do not write inline Python via Bash heredoc to compute dates or write JSON.
- Do not create temporary scripts, notebooks, debug logs, or intermediate files.

One invocation, one target date, one upserted recommendation.
