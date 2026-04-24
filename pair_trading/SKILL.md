---
name: pair_trading
description: >
  Executes a daily pair-trading decision task over a fixed 3-month window by
  querying offline market data via the `pair_trading_mcp` server. On the first
  actual trading day, it selects one pair from the configured stock pool using
  only information visible by that day. It then trades that fixed pair day by day
  with strict chronological processing and no future data leakage, writing one
  completed structured JSON result to `results/pair_trading/`.

  Use this skill whenever the user asks you to run a pair trading task, select a
  stock pair and trade it, execute a pair trading simulation, or produce a pair
  trading results JSON - phrased as "run the pair trading experiment", "do pair
  trading", "select a pair and trade it", or "process the pair trading data".
---

# Pair Trading Skill

You are executing a daily-frequency **pair trading** task over the fixed target
window:

```
2025-03-01 through 2025-05-31 inclusive
```

You call MCP tools on the `pair_trading_mcp` server to read the configured stock
pool, pair-selection context, pair market context, common trading dates, current
pair prices, and deterministic pair-position mechanics. The server reads local
offline data only. It uses the DuckDB at `PAIR_TRADING_DB_PATH`, defaulting to
`trading/env/trading_env.duckdb`

Do not read DuckDB files directly. Everything you know about the
market must come from the MCP tools described below.

Your job has two stages:

1. **Pair selection stage**: on the first actual trading day in the target
   window, choose one ordered pair from the configured stock pool using only
   information visible up to and including that selection date.
2. **Pair trading stage**: trade that selected pair day by day through the target
   window, outputting one allowed pair action per processed trading day.

Integrity of the simulation depends on never using future information.

---

## Data Access - MCP Tools

Six tools are available on the `pair_trading_mcp` server:

| Tool | Purpose |
|---|---|
| `get_stock_pool()` | Returns the configured pool metadata: `{pool_name, symbols, backend, db_path, data_dir, file_pattern}`. The default pool is `AAPL`, `ADBE`, `AMZN`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`, but `PAIR_TRADING_STOCK_POOL` may override it. |
| `get_common_trading_dates(date_start, date_end, symbols?)` | Returns sorted dates where every requested symbol has a non-null price. Use this to find the first trading day and the dates to process for the selected pair. |
| `get_pair_selection_context(date_start, date_end, symbols?)` | Returns rows for each requested pool symbol over `[date_start, date_end]`. Rows contain `{symbol, date, price, news, 10k, 10q, momentum}`. Use only with `date_end <= selection_date`. |
| `get_pair_market_context(left, right, date_start, date_end)` | Returns visible rows for the fixed pair over `[date_start, date_end]`, keyed by symbol, with the same row fields. Use for daily reasoning with `date_end <= current_day`. |
| `get_pair_prices(left, right, target_date)` | Returns `{available, date, prices}` for the selected pair on `target_date`. If either side has no price, `available=false`. |
| `apply_pair_action(left, right, action, prices, current_position?)` | Applies deterministic old pair-trading semantics. `LONG_SHORT` opens long left / short right; `SHORT_LONG` opens short left / long right; `HOLD` keeps the existing position; `CLOSE` returns no position. |

### Row Shape

Rows returned in selection and market context have:

| Field | Meaning |
|---|---|
| `symbol` | Ticker symbol |
| `date` | Trading date, `YYYY-MM-DD` |
| `price` | Canonical close/adjusted close price from the backend |
| `volume` | Present for DuckDB-backed rows when available |
| `news` | List of news items. DuckDB rows use objects like `{title, highlights}` |
| `10k` | List of 10-K filing excerpts or objects visible on that date |
| `10q` | List of 10-Q filing excerpts or objects visible on that date |

The backend normalizes numpy arrays and missing values into JSON-safe lists, so
do not write direct list handling logic in the skill execution.

---

## No-Look-Ahead Discipline

The local dataset may contain rows after the day you are deciding. Your tool
calls must not request data beyond the current decision date:

- Pair selection: `get_pair_selection_context(..., date_end=selection_date)`.
- Daily trading: `get_pair_market_context(..., date_end=current_day)`.
- Do not call any tool over the full future target window before making a
  current-day decision, except `get_common_trading_dates`, which returns only the
  calendar of dates with prices and not market signals.

Never choose the pair using future returns, future spread behavior, future news,
or future filings. Never revise earlier decisions after seeing later data.

---

## Stage 1 - Pair Selection

1. Call `get_stock_pool()` and use its `symbols` as the stock universe.
2. Call `get_common_trading_dates("2025-03-01", "2025-05-31", symbols=pool)` to
   find dates where every pool member has a price. The first returned date is the
   `selection_date`.
3. Call `get_pair_selection_context("2025-01-01", selection_date, symbols=pool)`.
4. Select exactly two distinct stocks from the pool and keep them in a fixed
   order `(left, right)` for the whole run.

For pair selection, emphasize information visible by `selection_date`: recent
price history, current-day or recent news, same-day momentum if available, and
any `10k` / `10q` excerpts dated on or before `selection_date`. A good pair is a
relative-value candidate, for example a stronger name versus a weaker name, two
comparable companies with diverging visible news, or one name with a positive
catalyst against another with negative or weaker signals.

Do not compute advanced statistical pair metrics using the full March-May target
window. The pair is selected once and never changed.

---

## Stage 2 - Daily Pair Trading Loop

After selecting the pair, call:

```
get_common_trading_dates("2025-03-01", "2025-05-31", symbols=[left, right])
```

Process the returned dates chronologically. For each `current_day`:

1. Call `get_pair_market_context(left, right, date_start, current_day)`, where
   `date_start` is an earlier visible context date such as `"2025-01-01"` or a
   recent lookback start. The `date_end` must be `current_day`.
2. Call `get_pair_prices(left, right, current_day)`. If `available=false`, skip
   the day.
3. Reason only over rows dated `<= current_day`.
4. Choose exactly one daily action: `LONG_SHORT`, `SHORT_LONG`, or `HOLD`.
5. Optionally call `apply_pair_action(...)` to maintain the deterministic
   current position snapshot, especially when deciding whether `HOLD` means keep
   an existing exposure.
6. Append the record in memory and move to the next date. Do not change previous
   records.

### Action Semantics

If the fixed pair is:

```
("META", "MSFT")
```

then:

- `LONG_SHORT` means long META, short MSFT.
- `SHORT_LONG` means short META, long MSFT.
- `HOLD` means keep the existing pair position unchanged or initiate no new
  exposure if there is no current position.
- `CLOSE` means exit the existing pair position.

Always state the long leg and short leg explicitly in `trajectory` when the
action opens or maintains directional exposure.

---

## Signals and Reasoning

Each daily `trajectory` should briefly explain:

1. which pair remains active and why it was originally selected,
2. what visible signals you saw today for both stocks,
3. which side looks stronger or weaker, or why there is no clear edge,
4. the chosen action and one-sentence rationale.

Two to three sentences is enough. Ground every statement in data returned by MCP.

Optional lightweight heuristics are allowed when computed only from visible
history up to `current_day`:

- compare current and recent price trends for the two legs,
- compare today's or recent news tone and catalyst strength,
- compare momentum labels when available,
- check visible filings for materially positive or negative implications,
- avoid opening/changing exposure when signals conflict or are too thin.

---

## Output - Final JSON

Write a single JSON file at the end:

```
results/pair_trading/{agent_name}_pair_trading_{pair}_{model}.json
```

where:

- `agent_name` is your name, e.g. `codex` or `claude-code`,
- `pair` is the fixed pair label, e.g. `META_MSFT`,
- `model` is the actual model identifier from your system context.

Sanitize filename parts by replacing any character that is not alphanumeric,
`-`, or `_` with `_`; lowercase the model name.

Example:

```json
{
  "status": "completed",
  "start_date": "2025-03-03",
  "end_date": "2025-05-30",
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
      "trajectory": "Pair remains META, MSFT after the first-day selection because META had stronger visible catalysts than MSFT. Today META has firmer price action and more positive news while MSFT is comparatively neutral. Decision: LONG_SHORT - long META, short MSFT."
    }
  ]
}
```

### Field Rules

| Field | Rule |
|---|---|
| `status` | `"completed"` if all selected-pair trading dates were processed; `"partial"` if stopped early |
| `start_date` | First date actually processed |
| `end_date` | Last date actually processed |
| `model` | The model identifier |
| `recommendations[].pair` | Pair string in fixed order, e.g. `"META, MSFT"` |
| `recommendations[].date` | Trading date string `YYYY-MM-DD` |
| `recommendations[].price` | Object mapping each leg to its current `price` |
| `recommendations[].recommended_action` | Exactly `"LONG_SHORT"`, `"SHORT_LONG"`, or `"HOLD"` |
| `recommendations[].trajectory` | Traceable daily reasoning, usually 2-3 sentences |

Write the file once after all decisions are made. Accumulate decisions in memory.

---

## What NOT To Do

- Do not read DuckDB directly.
- Do not use `trading_mcp`; this skill uses `pair_trading_mcp`.
- Do not use future market signals to select the pair or decide a daily action.
- Do not change the selected pair after the first trading day.
- Do not compute statistics across the full future window before trading.
- Do not create temporary `.py` files, notebooks, debug logs, or intermediate files.
- Do not output multiple result files for one run.

---

## Implementation Approach

1. Resolve the pool with `get_stock_pool()`.
2. Find `selection_date` with `get_common_trading_dates` over the full pool and
   target window.
3. Fetch selection context from `2025-01-01` through `selection_date` and choose
   one ordered pair.
4. Find common trading dates for the selected pair over the target window.
5. Loop chronologically. For each date, fetch only context ending on that date,
   fetch current pair prices, decide the action, optionally update current
   position with `apply_pair_action`, and append one recommendation.
6. Write one final JSON file under `results/pair_trading/`.

One run produces one completed pair-trading JSON file.
