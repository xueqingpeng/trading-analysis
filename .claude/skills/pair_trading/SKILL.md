---
name: pair_trading
description: >
  Executes a daily pair trading decision task over a 3-month window using parquet
  data files that contain price, news, 10-K, 10-Q, and momentum fields for a fixed
  pool of 10 stocks. On the first trading day, selects one pair from the stock pool
  using only information visible on that day (especially first-day news across all
  10 stocks). Then trades that selected pair day by day for the rest of the window
  with strict chronological processing and no future data leakage, writing one final
  structured JSON result to results/pair_trading/.

  Use this skill whenever the user asks you to run a pair trading task, select a
  stock pair and trade it, execute a pair trading simulation, or produce a pair
  trading results JSON — even if they phrase it as "run the pair trading experiment",
  "do pair trading on the 10 stocks", "select a pair and trade it", or "process the
  pair trading data".
---

# Pair Trading Skill

You are executing a daily-frequency **pair trading** decision task over a 3-month
window (`2025-03-01` to `2025-05-31`) using the same parquet input format as the
single-stock trading task.

Your job has two stages:

1. **Pair selection stage**: on the **first trading day only**, choose **one pair of
   stocks** from the 10-stock pool using only information from `2025-01-01` to `2025-02-28`.
2. **Pair trading stage**: trade that selected pair day by day for the full 3-month
   window, outputting one of the allowed pair actions per trading day.

Integrity of the simulation depends entirely on you never using future information.
Read this skill carefully before starting.

For the input and output data, the user will provide a path to the input files and a
path to the output. For example:

```
Please run the pair trading task. The input data is at /data/trading,
please save the output json to /results/pair_trading.
```

---

## Setup

### Identify the target ticker

The fixed stock pool contains these 10 tickers:

`NVDA`, `TSLA`, `AAPL`, `AMZN`, `GOOGL`, `META`, `MSFT`, `BMRN`, `CRM`, `ADBE`

The data file for ticker `XYZ` lives at:

```
data/trading/XYZ-00000-of-00001.parquet
```

You must load data from `2025-01-01` to `2025-02-28` for all 10 stocks on the first trading day, because pair selection depends on comparing
them on the first trading day.

### Identify the model name

The output filename encodes which model produced the results. Use the actual model
identifier from your system context (e.g., `claude-sonnet-4-6`). Sanitize both
`ticker` and `model` for use in filenames: replace any characters that are not
alphanumeric, `-`, or `_` with `_`, and lowercase the model name.

Example: `claude-code_trading_AAPL_claude-sonnet-4-6.json`

### Ensure the output directory exists

You can create the following folder to save the output:

```
results/pair_trading/
```

Create it if it doesn't exist yet.

---

## Loading the data

Use Python + pandas to read the parquet file:

```python
import pandas as pd

tickers = ["NVDA", "TSLA", "MSFT", "AAPL", "AMZN", "GOOGL", "META",  "ADBE", "BMRN", "CRM"]

data = {}
for ticker in tickers:
    df = pd.read_parquet(f"data/trading/{ticker}-00000-of-00001.parquet")
    df = df.sort_values("date").reset_index(drop=True)
    data[ticker] = df
```

**Schema** — each row is one calendar date for the ticker:

| Field      | Type            | Notes |
|------------|-----------------|-------|
| `date`     | string          | e.g., `"2025-01-02"` |
| `asset`    | string          | ticker symbol |
| `prices`   | float64         | daily price (use as close price) |
| `news`     | list[string] / ndarray | news headlines/summaries for that day |
| `10k`      | list[string] / ndarray | 10-K excerpts (may be empty most days) |
| `10q`      | list[string] / ndarray | 10-Q excerpts (may be empty most days) |
| `momentum` | string          | directional label, e.g. `"up"`, `"down"`, `"neutral"` |

**Important:** After `read_parquet`, the list fields (`news`, `10k`, `10q`) may come
back as `numpy.ndarray` objects, not Python `list`. Do **not** check `isinstance(x,
list)` to decide if a field is empty. Instead use:

```python
import numpy as np

def is_nonempty(val):
    if val is None:
        return False
    if isinstance(val, (list, np.ndarray)):
        return len(val) > 0
    return bool(val)
```

---

## Stage 1 — Pair selection on the first trading day

### First trading day rule

You must select the pair **once**, on the **first trading day in the target window**.

The target window is:

```
2025-03-01 through 2025-05-31 inclusive
```

And the news use to select the pairs is from `2025-01-01` to `2025-02-28`, Please note that all news items are given the same weight to make the pair selection, 
regardless of whether they appear early or late.
Because weekends and holidays may not appear in the data, determine the actual first trading day by finding the earliest date in this window that appears in the dataset.

#### What information may be used for pair selection

For **pair selection only**, you may use:

1. each stock's visible history up to and including the first trading day
2. each stock's news on the first trading day
3. each stock's momentum on the first trading day
4. any `10k` / `10q` excerpts available on or before the first trading day

The intended emphasis is: **select the pair based on all 10 stocks' news on the first day**, while optionally using same-day momentum and already-visible filing context as supporting information.

#### What pair to select

Choose exactly **two distinct stocks** from the 10-stock pool.

The pair should be selected because, based on the first-day visible signals, it looks like a good candidate for a relative-value trade over the upcoming period. For example, the first-day signals may suggest:

1. one stock appears relatively stronger and another relatively weaker
2. both stocks are in comparable large-cap tech ecosystems but have diverging news sentiment
3. one stock has positive momentum/news while another has negative or weaker signals
4. one stock has a catalyst and the other lacks one or faces negative pressure

You do **not** need to compute advanced statistical pair metrics over the full 3-month window. Do **not** use future spread behavior, future returns, or future correlation to choose the pair.

#### Pair selection output behavior

Once the pair is selected:

1. record it in memory
2. use the same pair for every trading day
3. never change the pair later
This is the heart of the task. Process days one at a time, in order. **Never read ahead.**

---

## Stage 2 — The trading loop for the selected pair

This is the heart of the task. After selecting the pair on the first trading day, process trading days one at a time, in order. **Never read ahead.**

```
for each trading day t in [2025-03-01 .. 2025-05-31]:
    1. For each stock in the selected pair, slice its dataframe to rows where date <= t
    2. Extract each stock's row for date == t; if either stock has no row on date t, skip that day
    3. Gather signals visible for both stocks on day t
    4. Reason over the pair relationship and decide one action
    5. Record the decision immediately — once recorded, never change it
    6. Move to t+1
```

Why does order matter so much? Because this task is measuring whether you can make
realistic trading decisions without peeking at future prices or future filings. Even
reading tomorrow's news in a pre-scan before making today's decision would be
cheating. Treat each day as if you are sitting at a terminal on that morning.

### Which days to process

Only process days where both selected stocks actually appear in the dataset within
the window. Skip weekends, holidays, and any day where one side of the pair has no
row.
The loop should cover the window `2025-03-01` through `2025-05-31` inclusive.

---

## Signals and reasoning

On each trading day, you have access to:

- **`prices`** — today's price, plus all historical prices you've seen so far
- **`momentum`** — today's directional label
- **`news`** — today's news items (could be empty)
- **`10k`** / **`10q`** — any earnings/filing excerpts released on or before today

### How to reason

### How to reason for pair trading

Each daily `trajectory` should briefly explain:

1. which pair was selected and why it remains the active pair
2. what signals you saw today for both stocks
3. which side looks stronger and which side looks weaker, or why neither side has a clear edge
4. the action you chose and a one-sentence rationale

You do not need to be exhaustive — 2 to 3 sentences is enough. The point is that the reasoning is traceable and grounded in the data actually visible on that day.

#### Allowed daily actions

For each trading day, output exactly one of these actions:

- **`LONG_SHORT`** — go long the relatively stronger stock and short the relatively weaker stock
- **`SHORT_LONG`** — short the first stock and go long the second stock
- **`HOLD`** — do not initiate or change relative exposure today

Use the `trajectory` text to make clear which stock is long and which is short.

#### Action semantics

If the pair is ordered as:

```
("META", "MSFT")
```

then:

- **`LONG_SHORT`** means: long META, short MSFT
- **`SHORT_LONG`** means: short META, long MSFT
- **`HOLD`** means: maintain no new directional pair action for the day

To avoid ambiguity, always state the long leg and short leg explicitly in the trajectory.

#### Good example

```
Pair: META, MSFT. META has positive product/news sentiment and up momentum today, while MSFT is neutral with weaker news flow. Relative signal favors META over MSFT. Decision: LONG_SHORT — long META, short MSFT.
```
#### Bad example (uses future info)

```
META will outperform MSFT next month, so I choose this pair and go long now.
```

#### Optional lightweight heuristics

You may use simple in-loop reasoning heuristics based only on visible history, such as:

- comparing today's momentum labels across the two selected stocks
- comparing the tone or strength of today's news across the two stocks
- checking whether either stock has a visible filing excerpt with notably positive or negative implications
- comparing short recent visible price trend using only prior and current rows

These heuristics must be computed only from data visible up to day `t`.

#### Do not

- fit models on the full future window
- compute future spread reversion statistics
- rank pairs using any information from dates after the first trading day
- use a later date to revise the initial pair choice

---

## Output format

Write a single JSON file to `results/pair_trading/{agent_name}_pair_trading_{pair}_{model}.json`, the `agent_name` is your name, e.g., "codex" or "claude-code":

```json
{
  "status": "completed",
  "start_date": "2025-03-03",
  "end_date": "2025-05-28",
  "model": "claude-sonnet-4-6",
  "recommendations": [
    {
      "pair": "META, MSFT",
      "date": "2025-03-03",
      "price": {
        "META": 182.45,
        "MSFT": 401.12
      },
      "recommended_action": "LONG_SHORT",
      "trajectory": "Pair selected on the first trading day from the 10-stock universe: META, MSFT. Today META shows stronger momentum and more positive news, while MSFT is comparatively neutral. Relative signal favors META. Decision: LONG_SHORT — long META, short MSFT."
    },
    {
      "pair": "META, MSFT",
      "date": "2025-03-04",
      "price": {
        "META": 180.10,
        "MSFT": 399.88
      },
      "recommended_action": "HOLD",
      "trajectory": "Pair remains META, MSFT based on the initial first-day selection. Today's momentum and news are mixed with no clear relative edge between the two names. Decision: HOLD to avoid a weak-conviction pair trade."
    }
  ]
}
```

**Field rules:**

| Field | Rule |
|-------|------|
| `status` | `"completed"` if all days processed successfully; `"partial"` if stopped early |
| `start_date` | First date actually processed |
| `end_date` | Last date actually processed |
| `model` | The model identifier (sanitized for filename use) |
| `recommendations[].pair` | Pair string in fixed order, e.g. `"META, MSFT"` |
| `recommendations[].date` | Trading date string `YYYY-MM-DD` |
| `recommendations[].price` | Object mapping each stock in the pair to its `prices` value for that day |
| `recommendations[].recommended_action` | Exactly one of: `LONG_SHORT"`, `"SHORT_LONG"`, `HOLD"` (uppercase) |
| `recommendations[].trajectory` | Your reasoning for that day (2-3 sentences) |


**Write the file once, at the end**, after all decisions are made. Accumulate
decisions in memory and write one final JSON.

---

## Important note on pair order

The pair order must remain fixed throughout the file. If you selected `("META", "MSFT")` on day 1, keep writing:

```json
"pair": "META, MSFT"
```
on every later day.

---

## What NOT to do

- Do not pre-scan the full 3-month period before selecting the pair
- Do not choose the pair using future returns, future spread behavior, or future news
- Do not change the selected pair after the first trading day
- Do not compute statistics across the whole future window before trading
- Do not create temporary `.py` files, notebooks, debug logs, or intermediate files
- Do not modify the result file once written
- Do not output multiple result files for the same run

If you need to compute something, do it in memory within the pair-selection step or within the chronological daily loop.

---

## Implementation approach

The cleanest approach is to write a short inline Python script via the Bash tool that:

1. loads all 10 parquet files
2. determines the first actual trading day in the target window
3. selects one stock pair using only first-day visible information across all 10 stocks
4. loops over later trading days chronologically for that fixed pair
5. collects `(pair, date, prices, action, trajectory)` records in memory
6. writes one final JSON file at the end

Keep all intermediate computation in memory. Do not save the script to disk.
