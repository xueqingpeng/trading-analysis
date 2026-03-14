---
name: reporting
description: >
  Generates a structured weekly equity research report for a single stock every
  Monday over a 3-month window, using parquet data files containing price, news,
  10-K, 10-Q, and momentum fields. Each report covers the prior week's price
  action, news, and filings, computes key metrics, and issues a BUY/SELL/HOLD
  rating with supporting analysis. Writes all weekly reports to a single Markdown
  file at results/reporting/.

  Use this skill whenever the user asks to generate an equity report, produce a
  weekly stock report, write a research note, or evaluate report generation on
  parquet data — even if they phrase it as "write a report for AAPL", "generate
  weekly reports for NVDA", or "produce equity research for MSFT".
---

# Report Generation Skill

You are generating weekly equity research reports for a single stock over a
3-month window (2025-03-01 to 2025-05-31). Every **Monday** in this window,
you write one structured report covering the **prior trading week** (the five
trading days ending the previous Friday).

Each report must summarize price action, news, and any filings from that week,
compute key metrics, and issue a **BUY / SELL / HOLD** rating with rationale.

Integrity of the reports depends on never using information beyond what was
available on the Monday the report is written. Read this skill carefully before
starting.

For input and output paths, the user will provide them directly. For example:
"The data is at `/data/trading`", "Please save reports to `/results/reporting`".

A typical user request looks like:

```
Please generate weekly equity reports for AAPL. The input data is at /data/trading,
please save the output to /results/reporting.
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
results/reporting/
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
| `date`     | datetime                | trading date |
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

Identify every **Monday** in the window `2025-03-01` through `2025-05-31` that
exists in the dataset. For each Monday, write one report using only data visible
on or before that date. **Never read ahead.**

```
for each Monday M in [2025-03-01 .. 2025-05-31]:
    1. Slice the dataframe to rows where date <= M  (no future data)
    2. Identify the prior week's trading days: the 5 trading days ending the
       Friday immediately before M (use dates actually present in the dataset)
    3. Compute metrics from the prior week's data
    4. Write the report for Monday M
    5. Move to the next Monday
```

If a Monday is not a trading day in the dataset (e.g., market holiday), use the
next available trading day in that week as the report date.

---

## Data available for each report

On Monday M, you may use:

- **Prior week's prices**: the 5 trading days ending the Friday before M
- **Prior week's news**: all news items from those 5 days
- **All filings up to M**: any `10k` or `10q` excerpts on or before M
- **Momentum**: the momentum label for the last trading day of the prior week
- **Historical prices**: all prices up to and including the Friday before M
  (for moving average and trend calculations)

---

## Required metrics (compute for each report)

| Metric | Definition |
|--------|-----------|
| `week_open` | Price on the first trading day of the prior week |
| `week_close` | Price on the last trading day (Friday) of the prior week |
| `week_high` | Highest price among the 5 prior trading days |
| `week_low` | Lowest price among the 5 prior trading days |
| `weekly_return_pct` | `(week_close - week_open) / week_open × 100`, rounded to 2 decimal places |
| `ma_4week` | Simple average of closing prices over the 20 trading days ending prior Friday (or fewer if insufficient history) |
| `ma_1week` | Simple average of closing prices over the 5 prior trading days |
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
State the rating: `BUY`, `SELL`, or `HOLD`.

Then provide 2–3 bullet points explaining the **investment thesis** — the core
reasons why the stock is attractive, unattractive, or neutral at this moment.
Each bullet should be a distinct, evidence-based argument grounded in the week's data.

Rating guidelines (apply to the prior week's evidence):
- **BUY**: positive weekly return + upward momentum + net positive news sentiment, or
  strong catalyst that outweighs weak price action
- **SELL**: negative weekly return + downward momentum + net negative news sentiment
- **HOLD**: mixed or conflicting signals; avoid forcing a directional view

Do not assign BUY on a week with sharply negative price action unless news or filings
strongly and specifically justify a reversal thesis.

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
Bullet-point summary of the **3–5 most significant news items** from the prior week.
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

Write a single **Markdown** file to:

```
results/reporting/{agent_name}_reporting_{ticker}_{model}.md
```

Example: `results/reporting/claude-code_reporting_AAPL_claude-sonnet-4-6.md`

The file contains a document header followed by one report section per Monday,
separated by horizontal rules. Use the exact template below:

---

````markdown
# Equity Research Report: {TICKER}

**Agent:** {agent_name} | **Model:** {model} | **Period:** 2025-03-01 to 2025-05-31

---

## Week of {week_start} to {week_end}
**Report Date:** {report_date} | **Rating:** ⬆ BUY / ➡ HOLD / ⬇ SELL

### 1. Executive Summary
{3–5 sentence paragraph}

---

### 2. Investment Rating & Thesis
**Rating: BUY / SELL / HOLD**

- {thesis bullet 1}
- {thesis bullet 2}
- {thesis bullet 3}

---

### 3. Weekly Price Performance & Technical Indicators

| Metric                  | Value        |
|-------------------------|--------------|
| Open                    | $227.45      |
| Close                   | $229.10      |
| Weekly Return           | +0.73%       |
| Week High               | $231.50      |
| Week Low                | $226.80      |
| Intra-week Volatility   | 2.07%        |
| 1-Week MA               | $228.60      |
| 4-Week MA               | $228.30      |
| Price vs 4-Week MA      | Above        |
| 4-Week Cumulative Return| -1.24%       |
| Momentum                | Neutral      |

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

---
````

**Format rules:**

| Element | Rule |
|---------|------|
| Rating emoji | ⬆ for BUY, ➡ for HOLD, ⬇ for SELL — on the week header line |
| Metrics table | All 11 metrics required; use `$` prefix for prices, `%` suffix for returns |
| Prices | Round to 2 decimal places |
| Percentages | Include sign (`+0.73%`, `-1.24%`); round to 2 decimal places |
| News bullets | Bold the headline or topic as the bullet label |
| Section headers | Use exact `###` level shown; do not rename or reorder sections |
| Horizontal rules | `---` between each section and between each weekly report |
| Empty data | State explicitly ("No news this week.", "No filings available.") |

**Write the file once, at the end**, after all reports are generated. Accumulate
all weekly report content in memory and write one final Markdown file.

---

## What NOT to do

- Do not use any data beyond what was available on the Monday the report is written
- Do not invent price levels, news, or filing content not present in the parquet data
- Do not skip Mondays in the window without a reason (holiday = use next trading day)
- Do not write partial reports — all 8 sections must be present for every weekly report
- Do not rename or reorder the section headers
- Do not leave the metrics table incomplete — compute all 11 metrics from available history
  even if the window is shorter than 20 days (use whatever history exists)
- Do not invent prices, news, or filing content not present in the parquet data
- Do not create temporary `.py` files, notebooks, debug logs, or intermediate files
- Do not write multiple output files for the same run
- Do not output raw JSON — the output must be a `.md` Markdown file

---

## Implementation approach

The cleanest approach: write a short inline Python script via the Bash tool that
loads the parquet, identifies all Mondays in the window, computes all 11 metrics for
each prior week, assembles the Markdown for each report section in memory, then
writes one final `.md` file. Keep all intermediate computation in memory. Do not
save the script to disk.
