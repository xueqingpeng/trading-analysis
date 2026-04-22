---
name: report_generation
description: >
  Generates a structured weekly equity research report for a single stock every
  Monday over a 3-month window, using parquet data files containing price, news,
  10-K, 10-Q, and momentum fields. Each report covers the prior week's price
  action, news, and filings, computes key metrics, and issues a graduated BUY/SELL/HOLD
  rating (Strong BUY → BUY → HOLD → SELL → Strong SELL) with supporting analysis.
  All weekly reports for a run are stored as individual .md files inside a single
  run-specific folder: results/report_generation/{agent_name}_report_generation_{ticker}_{model}/.

  Use this skill whenever the user asks to generate an equity report, produce a
  weekly stock report, write a research note, or evaluate report generation on
  parquet data — even if they phrase it as "write a report for AAPL", "generate
  weekly reports for NVDA", or "produce equity research for MSFT".
---

# Report Generation Skill

You are generating weekly equity research reports for a single stock over a
3-month window (2025-03-01 to 2025-05-31). Every **Monday** in this window,
you write one structured report covering the **prior calendar week** (the full
7-day period ending the previous Sunday, including weekends and holidays).

Each report must summarize price action, news, and any filings from that week,
compute key metrics, and issue a graduated **Strong BUY / BUY / HOLD / SELL / Strong SELL** rating with rationale.

Integrity of the reports depends on never using information beyond what was
available on the Monday the report is written. Read this skill carefully before
starting.

For input and output paths, the user will provide them directly. For example:
"The data is at `/data/trading`", "Please save reports to `/results/report_generation`".

A typical user request looks like:

```
Please generate weekly equity reports for AAPL. The input data is at /data/trading,
please save the output to /results/report_generation.
```

---

## Setup

### Identify the target ticker

The user will specify (or you can infer from context) which of the 10 available
tickers to report on:

`AAPL`, `ADBE`, `AMZN`, `BMRN`, `CRM`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`

The data file for ticker `XYZ` lives at:

```
data/trading/XYZ-00000-of-00001.parquet
```

### Identify the agent name and model

- `agent_name`: your agent name, e.g. `claude-code` or `codex`
- `model`: your model identifier from system context (e.g. `claude-sonnet-4-6`);
  sanitize for filename use (replace characters that are not alphanumeric, `-`, or
  `_` with `_`, lowercase)

### Ensure the output directory exists

```
results/report_generation/{agent_name}_report_generation_{ticker}_{model}/
```

Create it if it doesn't exist yet.

---

## Loading the data

Use Python + pandas to read the parquet file:

```python
import pandas as pd
df = pd.read_parquet("data/trading/TICKER-00000-of-00001.parquet")
df['date'] = pd.to_datetime(df['date'])
df = df.sort_values("date").reset_index(drop=True)
```

**Schema** — each row is one calendar date for the ticker:

| Field      | Type                    | Notes |
|------------|-------------------------|-------|
| `date`     | datetime                | calendar date; non-trading days may appear with news but NaN prices |
| `asset`    | string                  | ticker symbol |
| `prices`   | float64                 | daily close price |
| `news`     | list[string] / ndarray  | summarized news for that day |
| `10k`      | list[string] / ndarray  | 10-K excerpts (sparse) |
| `10q`      | list[string] / ndarray  | 10-Q excerpts (sparse) |
| `momentum` | string                  | `"up"`, `"down"`, or `"neutral"` |

**Important:** list fields may come back as `numpy.ndarray`. Use:

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

## The report generation loop

Identify every **Monday** in the calendar range `2025-03-01` through `2025-05-31`.
For each Monday, write one report using only data visible on or before that date.
**Never read ahead.**

```
for each Monday M in [2025-03-01 .. 2025-05-31]:
    1. Slice the dataframe to rows where date <= M  (no future data)
    2. Identify the prior calendar week: the 7-day period Mon–Sun immediately
       before M. Within that window:
         - Trading days (rows where `prices` is not NaN): used for price metrics
         - All rows with news (including weekends/holidays): used for news section
    3. Compute metrics from the prior week's data
    4. Write the report for Monday M
    5. Move to the next Monday
```

If a Monday is not a trading day in the dataset (e.g., market holiday), use the
next available trading day in that week as the report date.

---

## Data available for each report

On Monday M, you may use:

- **Prior week's prices**: the trading days (non-NaN price rows) within the prior calendar week
- **Prior week's news**: all news items from the full 7-day calendar period
  (Monday through Sunday of the prior week) — include weekend and holiday news
  even if no price data exists for those days
- **All filings up to M**: any `10k` or `10q` excerpts on or before M
- **Momentum**: the momentum label for the last trading day of the prior week
- **Historical prices**: all non-NaN price rows up to and including the last trading
  day of the prior week (for moving average and trend calculations)

---

## Required metrics (compute for each report)

| Metric | Definition |
|--------|-----------|
| `week_open` | Price on the first trading day of the prior week |
| `week_close` | Price on the last trading day of the prior week (not necessarily Friday if there was a holiday) |
| `week_high` | Highest price among the trading days within the prior calendar week |
| `week_low` | Lowest price among the trading days within the prior calendar week |
| `weekly_return_pct` | `(week_close - week_open) / week_open × 100`, rounded to 2 decimal places |
| `ma_4week` | Simple average of closing prices over the 20 trading days ending the last trading day of the prior week (or fewer if insufficient history) |
| `ma_1week` | Simple average of closing prices over the trading days within the prior calendar week |
| `price_vs_ma4` | `"above"` if `week_close > ma_4week`, `"below"` otherwise |
| `return_4week_pct` | `(week_close - price_20_days_ago) / price_20_days_ago × 100`, rounded to 2 decimal places (use earliest available if fewer than 20 days of history) |
| `weekly_volatility` | `(week_high - week_low) / week_open × 100`, rounded to 2 decimal places — measures intra-week price range as % of open |
| `momentum` | The momentum label from the last day of the prior week (`"up"`, `"down"`, `"neutral"`) |

---

## Report sections

Each report must contain all **8** of the following sections, following the structure
of professional equity research update notes (per CFA Institute and sell-side conventions):

### 1. Executive Summary
One paragraph (3–5 sentences) covering the single most important development of the
week — the dominant price move, key news catalyst, or filing highlight. State the
investment rating and a one-sentence thesis at the end.

### 2. Investment Rating & Thesis
State the rating using the 5-level scale:

| Rating | When to use |
|--------|-------------|
| **Strong BUY** | Evidence is clearly and broadly positive across multiple signals |
| **BUY** | Evidence leans positive but not all signals align |
| **HOLD** | Signals are genuinely mixed; no clear directional lean |
| **SELL** | Evidence leans negative but not uniformly so |
| **Strong SELL** | Evidence is clearly and broadly negative across multiple signals |

Then provide 2–3 bullet points explaining the **investment thesis** — the core
reasons for the rating. Each bullet should be a distinct, evidence-based argument
grounded in the week's data.

Apply your own analytical judgment — weigh price action, momentum, news sentiment,
and any filing signals holistically. **HOLD is not the safe default**; use it only
when you genuinely cannot determine a directional lean. Show your reasoning through
the thesis bullets.

### 3. Weekly Price Performance & Technical Indicators
Present all computed metrics in a structured, readable format:
- Open / Close / Weekly return %
- Week High / Low / Intra-week volatility %
- 1-week MA vs 4-week MA (whether the short MA is above or below the long MA indicates
  recent trend direction)
- Price vs 4-week MA: above or below
- 4-week cumulative return %
- Momentum label

### 4. News & Catalysts
Bullet-point summary of the **3–5 most significant news items** from the prior week,
covering the full 7-day calendar period (including weekends and holidays — news on
non-trading days is equally valid and must not be omitted).
Each bullet: one to two sentences — what happened and why it matters for the stock.
Group related items if the week had many similar stories.
If no news was available, state that explicitly.

### 5. Earnings & Filings Update
Summarize any `10-K` or `10-Q` excerpts that became available on or before Monday M.
Focus on content relevant to the investment thesis: revenue trends, margin commentary,
forward guidance, or balance sheet signals. If no filings are available, state that
explicitly.

### 6. Valuation Snapshot
Given the limited data available (price and filings only, no full financial statements),
provide a simplified valuation commentary:
- Note the stock's recent price trend relative to its 4-week MA as a momentum-based
  fair value signal
- If any financial data appears in `10-K` or `10-Q` excerpts (e.g., EPS, revenue,
  margins), compute or cite relevant multiples
- Comment on whether the stock appears stretched, fairly valued, or compressed
  relative to its recent trading range and any available fundamental data

### 7. Risk Factors
List 2–4 **specific, evidence-based** risks from the week's data — regulatory,
competitive, macro, operational, or sentiment risks visible in the news or filings.
Each risk should be one sentence. Avoid generic boilerplate; tie each risk to actual
content observed in the data.

### 8. Recommendation & Outlook
Restate the rating. Then 2–3 sentences: what specific factors to monitor in the
coming week, and what would cause a rating change (upside catalyst or downside trigger).
Base all outlook commentary strictly on information available as of Monday M.

---

## Output format

Write **one Markdown file per Monday report** to:

```
results/report_generation/{agent_name}_report_generation_{ticker}_{model}/{agent_name}_report_generation_{ticker}_{YYYYMMDD}_{model}.md
```

Where `{YYYYMMDD}` is the report date (the Monday). Example for the first report:

```
results/report_generation/claude-code_report_generation_AAPL_claude-sonnet-4-6/claude-code_report_generation_AAPL_20250303_claude-sonnet-4-6.md
```

A 3-month run covering ~13 Mondays produces **~13 separate `.md` files** in the
output directory — one per weekly report, analogous to how the trading skill
produces one decision record per trading day.

Each file is a single self-contained report. Use the exact template below:

---

````markdown
# Equity Research Report: {TICKER}

**Agent:** {agent_name} | **Model:** {model} | **Report Date:** {report_date}
**Week Covered:** {week_start} to {week_end} | **Rating:** {RATING_EMOJI} {RATING}

---

### 1. Executive Summary
{3–5 sentence paragraph}

---

### 2. Investment Rating & Thesis
**Rating: {RATING}**

- {thesis bullet 1}
- {thesis bullet 2}
- {thesis bullet 3}

---

### 3. Weekly Price Performance & Technical Indicators

| Metric                   | Value        |
|--------------------------|--------------|
| Open                     | $227.45      |
| Close                    | $229.10      |
| Weekly Return            | +0.73%       |
| Week High                | $231.50      |
| Week Low                 | $226.80      |
| Intra-week Volatility    | 2.07%        |
| 1-Week MA                | $228.60      |
| 4-Week MA                | $228.30      |
| Price vs 4-Week MA       | Above        |
| 4-Week Cumulative Return | -1.24%       |
| Momentum                 | Neutral      |

---

### 4. News & Catalysts
- **{headline}:** {1–2 sentence impact summary}
- **{headline}:** {1–2 sentence impact summary}

---

### 5. Earnings & Filings Update
{Summary of 10-K/10-Q content, or "No filings available as of {report_date}."}

---

### 6. Valuation Snapshot
{Paragraph: price vs MA commentary, any multiples from filings if available,
overall fair value assessment}

---

### 7. Risk Factors
- {Specific risk 1 tied to observed data}
- {Specific risk 2}
- {Specific risk 3}

---

### 8. Recommendation & Outlook
**{RATING}.** {2–3 sentences on what to monitor next week and what would trigger
a rating change.}
````

---

**Format rules:**

| Element | Rule |
|---------|------|
| Rating emoji | ⬆⬆ Strong BUY, ⬆ BUY, ➡ HOLD, ⬇ SELL, ⬇⬇ Strong SELL — on the header line |
| Metrics table | All 11 metrics required; use `$` prefix for prices, `%` suffix for returns |
| Prices | Round to 2 decimal places |
| Percentages | Include sign (`+0.73%`, `-1.24%`); round to 2 decimal places |
| News bullets | Bold the headline or topic as the bullet label |
| Section headers | Use exact `###` level shown; do not rename or reorder sections |
| Horizontal rules | `---` between each section within a report |
| Empty data | State explicitly ("No news this week.", "No filings available.") |

**Write each file immediately** after generating that Monday's report — do not
accumulate all reports in memory and write at the end.

---

## What NOT to do

- Do not use any data beyond what was available on the Monday the report is written
- Do not invent price levels, news, or filing content not present in the parquet data
- Do not skip any calendar Monday in the window without a reason (holiday = use next trading day)
- Do not write partial reports — all 8 sections must be present for every weekly report
- Do not rename or reorder the section headers
- Do not leave the metrics table incomplete — compute all 11 metrics from available history
  even if the window is shorter than 20 days (use whatever history exists)
- Do not create temporary `.py` files, notebooks, debug logs, or intermediate files
- Do not combine all weekly reports into a single output file — each Monday must produce its own `.md` file
- Do not write report files directly to `results/report_generation/` — all files must go inside the run-specific subfolder `{agent_name}_report_generation_{ticker}_{model}/`
- Do not output raw JSON — the output must be `.md` Markdown files

---

## Implementation approach

The cleanest approach: write a short inline Python script via the Bash tool that
loads the parquet, identifies all calendar Mondays in the window, then loops over them
chronologically. For each Monday, compute all 11 metrics, assemble the full
Markdown report in memory, and write it immediately to its own dated `.md` file
before moving to the next Monday. A 3-month window produces ~13 files. Do not
save the script to disk.
