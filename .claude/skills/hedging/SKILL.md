---
name: hedging
description: >
  Makes one daily hedging decision for a fixed ordered stock pair, or
  selects the fixed pair on the run's first day, by querying offline data
  through the `hedging_mcp` server. Each invocation handles exactly one target
  date and upserts one recommendation into `results/hedging/`. The same skill
  can be driven by an external date-loop for any contiguous run window
  (benchmark, live, or otherwise).

  Use this skill whenever the user asks for a hedging decision, pair
  selection, or one daily hedging step.
---

# Hedging Skill

You are making a **single-day** hedging decision. On the run's first day,
select one ordered pair from the configured pool using only information
visible by `TARGET_DATE`. On later days, trade the same fixed pair and upsert
exactly one result record.

Everything you know about market data must come from MCP tools on the
`hedging_mcp` server. Do not read DuckDB or parquet files directly.

---

## Inputs

The user invocation specifies:

1. **`TARGET_DATE`** — the day to decide on, `YYYY-MM-DD`. Optional. If
   omitted, resolve to the latest common trading date in the data:
   - `IS_FIRST_DAY=False` (pair already in file): call
     `is_pair_trading_day(left, right, today_or_any_recent_date)` and use
     `latest_common_date`.
   - `IS_FIRST_DAY=True` (no pair yet): call
     `get_common_trading_dates(today_minus_30d, today)` over the full pool
     and use the last returned date.
2. **`IS_FIRST_DAY`** — boolean, **default `False`**. The caller's assertion of
   whether `TARGET_DATE` is the run's first day:
   - `True` → the caller declares this is a brand-new run; agent runs pair
     selection (using only info ≤ `TARGET_DATE`), then writes the first record.
   - `False` → the caller declares the run is already in progress; agent reads
     the existing pair from the output file and only does daily hedging.

The pair `(LEFT, RIGHT)` is never passed in by the caller. It is determined
exactly two ways:
- `IS_FIRST_DAY=True` → agent runs pair selection and writes `(LEFT, RIGHT)`
  into the output file.
- `IS_FIRST_DAY=False` → agent reads `(LEFT, RIGHT)` from the existing output
  file.

Typical user phrasings:
- `start hedging on 2025-03-03` (→ `IS_FIRST_DAY=True`)
- `run hedging for 2025-03-04` (→ `IS_FIRST_DAY=False`, the default)

### IS_FIRST_DAY × output file consistency check

Before doing anything else, compare `IS_FIRST_DAY` to whether the output file
at `<output-root>/hedging_*_<model>.json` already exists:

| `IS_FIRST_DAY` | Output file exists? | Action |
|---|---|---|
| `True` | No | ✅ Proceed with pair selection. |
| `False` | Yes | ✅ Read pair from file, proceed with daily hedging. |
| `True` | **Yes** | **STOP**: report to user that running pair selection would overwrite the existing run. Tell them to delete the file or use a different `--output-root` if they really want a fresh run. |
| `False` | **No** | **STOP**: report to user that no existing pair is on disk; the first invocation of a run must pass `IS_FIRST_DAY=True`. |

In both stop cases, **do NOT call `upsert_hedging_decision.py`** — just report
the situation and exit.

---

## Data Access - MCP Tools

Tools on the `hedging_mcp` server. The list/get pair pattern keeps individual
tool results small and lets the agent fetch detail on demand — bulk fetches
of full news highlights or filing bodies can exceed the model context.

| Tool | Purpose |
|---|---|
| `get_stock_pool()` | Returns `{pool_name, symbols, backend, db_path, data_dir, file_pattern}`. The `symbols` array is the candidate set for pair selection. The other fields are informational only — never read DB / parquet directly using `db_path` / `data_dir`. |
| `get_common_trading_dates(date_start, date_end, symbols?)` | Sorted list of dates in `[date_start, date_end]` where **every** requested symbol has a price (defaults to the full pool). Use for calendar discovery — primarily to **derive `TARGET_DATE`** when the caller didn't supply one (the last returned date in a sane recent range becomes `TARGET_DATE`). |
| `is_pair_trading_day(left, right, target_date)` | Returns `{is_trading_day, reason, prices, prev_trading_day, prev_prices, latest_common_date, should_upsert}`. `reason ∈ {'trading_day','weekend','holiday','missing_leg','not_loaded'}`. Use this **first** every day — it covers weekday checks, missing-row checks, single-leg-stale checks, and exposes the latest common date. |
| `get_prices(symbol, date_start, date_end)` | Rows `{symbol, date, price, volume}` in the range. `price` is `adj_close` (the canonical trading price). Use for trend / volatility context on either leg. |
| `get_pair_prices(left, right, target_date)` | One-shot fetch of both legs' prices on `target_date`: `{available, date, prices}`. `available=false` if either leg is missing that day. |
| `list_news(symbol, date_start, date_end, preview_chars?=300)` | Compact news metadata: `{symbol, date, id, highlights_chars, highlights_preview}` — preview is the first `preview_chars` chars of the body (no full highlights). Use this first to scan the lead of each day's coverage. |
| `get_news_by_id(symbol, id)` | Full article for one id: `{symbol, date, id, highlights}`. Call after `list_news` for the days whose preview looks relevant. |
| `list_filings(symbol, date_start, date_end, document_type?)` | Compact filings metadata: `{symbol, date, document_type, mda_chars, risk_chars}` — no content. Use to decide if/which section is worth reading. |
| `get_filing_section(symbol, date, document_type, section, offset=0, limit=None)` | Fetch one section (`'mda'` or `'risk'`) of a specific filing. Omit `limit` for the whole section; use `offset/limit` to paginate long sections. Returns `{content, total_chars, offset, returned_chars, has_more, …}`. |
| `apply_pair_action(left, right, action, prices, current_position?)` | Optional helper for deterministic dollar-neutral pair-position math (`LONG_SHORT`, `SHORT_LONG`, `HOLD`, `CLOSE`). Returns the new position dict (or null on `CLOSE`). The output persists the daily lifecycle action, but not the derived share snapshot. |

### Helper Scripts

Use helper scripts via Bash instead of writing inline Python:

```bash
python3 .claude/skills/hedging/scripts/date_offset.py TARGET_DATE 7 30 60 365
```

```bash
python3 .claude/skills/hedging/scripts/upsert_hedging_decision.py \
    --left META --right MSFT --target-date 2025-03-03 \
    --left-price 182.45 --right-price 401.12 \
    --action LONG_SHORT \
    --model <your model id> \
    --output-root <whatever the caller specified, e.g. /io/slot1>
```

**Pass `--output-root` whenever the caller specified one in the invocation
(e.g. `/io/slot1`).** The default `results/hedging` (relative to cwd) is
rarely writable inside a sandbox, so omitting it usually causes a
`PermissionError`. The upsert script owns filename sanitization,
load-or-create, upsert by date, sorting, recomputing date bounds, and writing
JSON.

---

## No-Look-Ahead Discipline

The local dataset may contain rows after the day you are deciding. Your tool
calls must not request data beyond the current decision date:

- Pair selection: only use prices/news/filings with `date_end <= selection_date`
  (which equals `TARGET_DATE` on the run's first day).
- Daily trading: only use prices/news/filings with `date_end <= TARGET_DATE`.
- **Once `TARGET_DATE` is established**, every subsequent MCP call must
  respect `date_end <= TARGET_DATE`. The single exception is the bootstrap
  call to `get_common_trading_dates` used to *derive* `TARGET_DATE` when the
  caller didn't supply one — that call necessarily looks at the data calendar
  to figure out what day to use.
- Do not use future returns, future spread behavior, future news, or future
  filings to select the pair or decide an action.

---

## Pair Selection

Only run this when `IS_FIRST_DAY=True` and the output file does not yet exist
(see the consistency check above). The `selection_date` is simply
`TARGET_DATE` — that's the day the run begins, and pair selection must use
only information visible by then.

1. Call `get_stock_pool()` and use its `symbols`.
2. Set `selection_date = TARGET_DATE`. (No window math needed — TARGET_DATE
   itself is the cutoff for pair selection on the run's first day.)
3. For each candidate symbol, fetch compact visible context ending at
   `selection_date`. Pick windows that fit your analysis — common choices:
   - `get_prices(symbol, date_start, selection_date)` — last 30 / 60 / 120 trading days for trend
   - `list_news(symbol, date_start, selection_date)` — last 1 / 3 / 7 days (one row per day)
   - optionally `list_filings(symbol, date_start, selection_date)` — last 6 months / 1 year
4. Scan each day's `highlights_preview` from `list_news`, then call
   `get_news_by_id` for the days worth reading in full. Same idea for
   filings: check `mda_chars` / `risk_chars` from `list_filings`, then
   `get_filing_section` for the section(s) that look material.
5. Select exactly two distinct stocks and keep the fixed order `(left, right)`
   for the whole run.

A good pair is a relative-value candidate: one visibly stronger name versus a
weaker one, comparable companies with diverging catalysts, or a positive
catalyst on one side against negative or thinner signals on the other.

---

## Daily Hedging

For a fixed pair and `TARGET_DATE`:

1. Call `is_pair_trading_day(left, right, TARGET_DATE)` first.
   - `reason == "weekend"` or `"holiday"`: force `HOLD`, use returned prior
     prices, and skip further market-data fetching.
   - `reason == "not_loaded"`: stop and report to the user; do not upsert.
   - `reason == "missing_leg"`: stop and report which leg is unavailable; do
     not upsert unless the tool says `should_upsert=true`.
   - `reason == "trading_day"`: continue.
2. Fetch visible price context for both legs over a window you choose
   (e.g. last 30 / 60 / 120 trading days through `TARGET_DATE`).
3. Call `list_news` for both legs over a window you choose (last 1 / 3 / 7
   days; one row per day). Scan each day's `highlights_preview` and call
   `get_news_by_id` for the days worth reading in full.
4. Optionally `list_filings` (last 6 months / 1 year — 1 year already covers
   a 10-K plus three 10-Qs; older filings are stale) and then
   `get_filing_section` if fundamentals would materially affect the pair
   decision.
5. Choose exactly one daily action: `LONG_SHORT`, `SHORT_LONG`, `HOLD`, or
   `CLOSE`.
6. Optionally call `apply_pair_action` to maintain the deterministic position
   snapshot when deciding whether to keep, flip, open, or close exposure.
7. Run `upsert_hedging_decision.py` with the final action and prices.

### Action Semantics

For fixed pair `("META", "MSFT")`:

- `LONG_SHORT` means long META, short MSFT.
- `SHORT_LONG` means short META, long MSFT.
- `HOLD` means keep existing exposure unchanged or initiate no exposure if
  there is no current position.
- `CLOSE` means close any existing pair exposure and remain flat. If there is
  no current position, `CLOSE` is equivalent to staying flat.

Output recommendations must use exactly `LONG_SHORT`, `SHORT_LONG`, `HOLD`,
or `CLOSE`.

Treat the recommendation stream as a persistent position lifecycle:

- Repeating `LONG_SHORT` or `SHORT_LONG` maintains that directional exposure.
- Switching between `LONG_SHORT` and `SHORT_LONG` flips the pair exposure.
- `HOLD` leaves the current position unchanged.
- `CLOSE` exits the current position and leaves the run flat until a later
  `LONG_SHORT` or `SHORT_LONG` opens new exposure.

---

## Output

The upsert script writes:

```
results/hedging/hedging_{LEFT}_{RIGHT}_{model}.json
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
      "recommended_action": "LONG_SHORT"
    }
  ]
}
```

The caller decides when to mark the run completed; daily invocations should
leave `status` as `in_progress` unless explicitly asked to finalize.

---

## What NOT To Do

- Do not read DuckDB or parquet files directly.
- Do not use `trading_mcp`; this skill uses `hedging_mcp`.
- Do not call data tools with `date_end > TARGET_DATE` for a decision.
- Do not pull all news highlights or filing bodies up front.
- Do not change the fixed pair after it has been selected.
- Do not compute statistics across future trading days before deciding.
- Do not write inline Python via Bash heredoc to compute dates or write JSON.
- Do not create temporary scripts, notebooks, debug logs, or intermediate files.

One invocation, one target date, one upserted recommendation.
