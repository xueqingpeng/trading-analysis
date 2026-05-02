---
name: report_generation
description: >
  Generates one standalone WEEKLY equity research report for one symbol on
  one target week-ending date. Uses the report_generation_mcp server to read
  offline DuckDB market, news, filings, and peer benchmark data, then writes
  one Markdown report plus a run-level summary JSON.
---

# Weekly Report Generation Skill

You are generating a **single-week equity research report** for one symbol,
covering the trading week ending on `TARGET_DATE` (typically a Friday).

You must use MCP tools on the `report_generation_mcp` server to read offline
DuckDB data. Then you reason over the evidence and write one structured
report through `upsert_report.py`.

Everything you know about the market must come from the MCP tools described
below. Do not call external APIs. Do not use network data.

---

## What this report is for

This is a **weekly investor letter**, not a daily trading note.

A weekly report's value comes from putting the stock in market context:

- Did it move with or against its sector this week?
- Is the trend confirmed by momentum indicators?
- How sensitive is it to broader market moves (beta)?
- What's the structural setup heading into next week?

That is why the metric set is balanced toward **relative performance and
beta**, not pure individual-stock alpha:

| Block | Count | Purpose |
|---|---|---|
| Alpha (price / trend / position) | 8 | Where the stock is on its own |
| Momentum signals | 3 | Direction confirmation (MACD, RSI, MA cross) |
| **Beta (relative to sector)** | **5** | **How it moved vs the market** |
| **Total** | **16** | |

The Section 3 table additionally displays four **context fields**
(`support_20d`, `resistance_20d`, `volume_ratio`, `cmf_20day`) alongside the
16 above. They are not part of the analytical core — they are levels and
participation cues displayed for the technical-synthesis narrative.

---

## Inputs

The user invocation specifies:

1. `SYMBOL` — one of:
   `AAPL`, `ADBE`, `AMZN`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`
2. `TARGET_DATE` — the **last trading day of the report week**, in
   `YYYY-MM-DD` format. This is typically a Friday but can be Thursday in a
   short week or any other valid trading day. Optional. If omitted, call
   `is_trading_day(SYMBOL, <best guess>)` and use the returned
   `latest_date_in_db`.

The "report week" is the **trading-day window from the Monday of
`TARGET_DATE`'s ISO calendar week through `TARGET_DATE` (inclusive)**.
Concretely the window starts at `TARGET_DATE - TARGET_DATE.weekday()` days,
so Friday TARGET_DATE → Mon-Fri (5 trading days), Thursday TARGET_DATE
(short week, e.g. Friday is a holiday) → Mon-Thu (4 trading days). A
mid-week holiday (e.g. Wednesday closed) further reduces the count.
Anchoring on Monday avoids pulling the prior week's Friday into "this
week" when TARGET_DATE is not a Friday.

Typical user phrasings:

- `weekly report for AAPL for week ending 2025-03-07`
- `write weekly report for TSLA 2025-04-11`
- `weekly report NVDA`

---

##  No Look-ahead

The MCP server enforces a hard cap on look-ahead in **two layers**, not just
in this prompt:

1. **Range queries** (`get_prices`, `list_news`, `list_filings`,
   `get_indicator`) — `date_end` is silently clamped to `as_of_date` at the
   SQL layer.
2. **Point queries** (`get_weekly_metrics`, `get_news_by_id`) — the tool
   raises `ValueError` if `target_date > as_of_date`.

You physically cannot read data after `TARGET_DATE`.

---

## Data Access

Use tools on the `report_generation_mcp` server. The table is ordered by
the **typical call sequence** for one weekly report; see "Recommended
Workflow" below for the full pattern. The last two tools (`get_prices`,
`get_indicator`) are raw-access escape hatches — `get_weekly_metrics`
already produces the 16 required metrics, so reach for these only when
you want OHLCV context outside the metrics or a non-standard indicator
parameter (e.g. RSI(7) instead of RSI(14)).

| Tool | Purpose |
|---|---|
| `is_trading_day(symbol, target_date)` | Gate the date — checks weekend / holiday / out-of-range in one call. |
| `get_weekly_metrics(symbol, target_date)` | Return ALL 16 required weekly metrics in one call. **Always use this; never recompute manually.** |
| `list_news(symbol, date_start, date_end, preview_chars?)` | Scan news in any window as **previews** (`{symbol, date, id, highlights_chars, highlights_preview}`). Default `preview_chars=600`. **You choose the lookback window** based on what context the week's price action demands: ~7 days for routine weeks, 14/30 days or longer for earnings cycles, M&A, delayed reactions, or multi-week narratives. Compute past dates via `date_offset.py` (see workflow step 3) — do not do date arithmetic in your head. |
| `get_news_by_id(symbol, id)` | Full article body (`highlights`) for one id. Standard drill-down step after `list_news` for items whose preview looks material. |
| `list_filings(symbol, date_start, date_end, document_type?)` | Filing metadata (`{date, document_type, mda_chars, risk_chars}`) — no content. Use to find the most recent 10-K / 10-Q on or before `TARGET_DATE` and decide which section is worth reading. |
| `get_filing_section(symbol, date, document_type, section, offset=0, limit=None)` | Read a specific filing section (`'mda'` or `'risk'`). Pass `limit=2500` for a preview, omit `limit` for full text, or use `offset`/`limit` to paginate long sections. |
| `list_peers(symbol)` | Static peer list and sector label for thesis framing. |
| `get_prices(symbol, date_start, date_end)` | Raw OHLCV when you need context beyond the 16 metrics. |
| `get_indicator(symbol, date_start, date_end, indicator, length?)` | Custom indicator series with non-standard parameters. |

### Rules

- **Never compute metrics in ad-hoc Python.** Always use `get_weekly_metrics`.
- For news and filings, follow the **preview-then-drill** pattern: scan with
  `list_news` / `list_filings`, then call `get_news_by_id` /
  `get_filing_section` only on the items worth reading in full. Pulling
  every article's full body is wasteful.
- Do not read duckdb directly.
- Do not write ad-hoc scripts to disk.

---

## Recommended Workflow

1. **Gate the date.** Call `is_trading_day(SYMBOL, TARGET_DATE)`.
   - `weekend` / `holiday` → The tool returns the most recent valid trading day in `prev_trading_day`. Do NOT guess or compute date math yourself. Instantly adopt `prev_trading_day` as your new `TARGET_DATE` and proceed to generate the report.
   - `not_loaded` → stop and report to user.
   - `trading_day` → continue.

2. **Pull all 16 metrics in one call.** Call
   `get_weekly_metrics(SYMBOL, TARGET_DATE)`. Save the entire dict; you'll
   reference these values in Sections 1, 2, 3, 6, and 7.

3. **Scan news, then drill down.** Decide on a lookback window based on
   what's likely to explain this week's price action: 7 days for a routine
   week, 14/30 days when an earnings cycle, guidance update, or M&A may
   be in play, longer when the stock has been driven by a multi-week
   narrative. There is no fixed default — pick the window that captures
   the catalysts that actually matter. Compute `date_start` via the
   bundled helper rather than doing date math yourself:

   ```bash
   python3 .claude/skills/report_generation/scripts/date_offset.py TARGET_DATE 7 30
   ```

   It prints one `<days>\t<YYYY-MM-DD>` line per offset. Then call
   `list_news(SYMBOL, date_start, TARGET_DATE)`. This returns **previews**
   (first 600 chars per item by default) plus `highlights_chars`, not
   full bodies. Read the previews to pick items that materially matter
   for the thesis, then call `get_news_by_id(SYMBOL, id)` for each one
   whose preview shows it's worth reading in full. For a weekly report
   it is normal to drill into 2–4 items; pulling every body is wasteful.

4. **Find and read the latest filing.** Compute `date_start = TARGET_DATE
   - 365 days` via `date_offset.py` (one year covers a 10-K plus three
   10-Qs) and call `list_filings(SYMBOL, date_start, TARGET_DATE)`. Pick the most recent row to get
   `(filing_date, document_type, mda_chars, risk_chars)`. Then call
   `get_filing_section(SYMBOL, filing_date, document_type, 'mda', limit=2500)`
   for the MD&A preview and another with `section='risk'` for Risk
   Factors. Drop `limit` (or paginate via `offset`) if you need more text.
   If `list_filings` returns empty, no 10-K/10-Q is available on or before
   `TARGET_DATE` — say so in Section 5.

5. **Pull peer / sector context.** Call `list_peers(SYMBOL)`.

6. **(Optional)** Call `get_indicator` only for non-canonical parameters
   (e.g., RSI(7) for a short-term overbought check) or time series.

7. **Synthesize and write.** Assemble Markdown using the structure in
   "Report Sections" below; call `upsert_report.py` to persist.

---

## Required Metrics (16 total)

Each report MUST present all 16 in Section 3. Always retrieve them via
`get_weekly_metrics`; the keys below match the dict keys returned.

### Alpha block (8) — where the stock is on its own

| Key | Definition |
|---|---|
| `week_open` | Open price of the first trading day in the report week |
| `week_close` | `adj_close` on `TARGET_DATE` |
| `weekly_return_pct` | `(week_close − prev_week_close) / prev_week_close × 100`. The base is the last `adj_close` BEFORE the report week. |
| `return_4week_pct` | `(week_close − close_20_trading_days_ago) / close_20_trading_days_ago × 100`. Approximates one month. |
| `ma_20day` | Average `adj_close` over the trailing 20 trading days |
| `price_vs_ma20` | `"above"` if `week_close > ma_20day`, else `"below"` |
| `weekly_volatility` | `stdev(daily_returns_in_week) × √5 × 100` — annualized-by-week |
| `dist_from_52w_high_pct` | `(week_close − 52w_high) / 52w_high × 100`. Always ≤ 0; near 0 = stock is near its 52-week top |

### Momentum block (3) — direction confirmation

| Key | Definition |
|---|---|
| `momentum_short` | `"up"` if `ma_5day > ma_20day`, `"down"` if smaller, `"neutral"` if equal. Short-term trend direction |
| `macd_signal` | One of `"bullish_strengthening"`, `"bullish_weakening"`, `"bearish_strengthening"`, `"bearish_weakening"`, `"neutral"`. Based on MACD(12, 26, 9). See "Reading MACD signals" below |
| `rsi_14` | 14-period RSI value (numeric). Companion field `rsi_class` is `"overbought"` (>70), `"oversold"` (<30), or `"neutral"` |

### Beta block (5) — how the stock moved vs the market

The benchmark is an **equal-weighted basket of the symbol's sector peers**
from `PEER_MAP` (returned in `benchmark_basket` for transparency). If the
symbol's `benchmark_basket` comes back empty (no peers for that symbol are
loaded in the current DB), all 5 beta-block metrics are `null`; explicitly
note this limitation in Section 6 of those reports.

| Key | Definition |
|---|---|
| `sector_basket_return_1w_pct` | Equal-weighted sector basket's weekly return, same window as the symbol's `weekly_return_pct` |
| `relative_return_1w_pct` | `weekly_return_pct − sector_basket_return_1w_pct`. **Positive = outperformed sector; negative = underperformed.** This is the headline beta metric |
| `relative_return_4w_pct` | Same as above but over 4 weeks (~20 trading days). Catches the medium-term trend in relative strength |
| `correlation_60d` | 60-day rolling correlation between symbol's daily returns and basket's daily returns. High (>0.7) = moves with the sector; low (<0.4) = idiosyncratic |
| `beta_60d` | 60-day rolling beta = `cov(symbol_returns, basket_returns) / var(basket_returns)`. **β > 1 = amplifies the sector**; β < 1 = dampened; β ≈ 0 = unrelated |

### Context fields (returned by `get_weekly_metrics`, not part of the 16)

The first four are **displayed in the Section 3 table** alongside the 16
core metrics — the remainder are returned for prose / thesis use only.

| Key | Use |
|---|---|
| `support_20d` | Lowest `low` over the trailing 20 trading days. Displayed as the near-term support reference. |
| `resistance_20d` | Highest `high` over the trailing 20 trading days. Displayed as the near-term resistance reference. |
| `volume_ratio` | This week's avg daily volume / trailing-20-day avg volume. `>1` = above-average activity; `<1` = quiet week. Displayed in Momentum & Volume row. |
| `cmf_20day` | Chaikin Money Flow over the trailing 20 trading days. Positive = net buying pressure; negative = net selling. Displayed in Momentum & Volume row. |
| `ma_5day` | Used internally for `momentum_short`; available for thesis prose if helpful |
| `macd_values` | `{line, signal, hist}` raw MACD numerics |
| `rsi_class` | The bucketed RSI label, see above |
| `week_trading_days` | Trading days actually in the window (5 normally, fewer in a holiday week) |
| `benchmark_basket` | The list of peer symbols used; `[]` means no benchmark available |

### Reading MACD signals

The `macd_signal` field collapses MACD into a 5-class regime:

- `bullish_strengthening` — MACD line above signal line **and** histogram is
  growing → uptrend strengthening
- `bullish_weakening` — MACD line above signal line **but** histogram is
  shrinking → uptrend losing momentum
- `bearish_strengthening` — MACD line below signal line **and** histogram is
  growing more negative → downtrend strengthening
- `bearish_weakening` — MACD line below signal line **but** histogram
  recovering toward zero → downtrend losing force
- `neutral` — MACD line essentially equal to signal line

### Reading the beta block

Three quick patterns to watch for:

| Pattern | Reading |
|---|---|
| `weekly_return_pct < 0` but `relative_return_1w_pct > 0` | Stock fell, but **less than the sector** — defensive outperformance |
| `correlation_60d` low **and** `relative_return_4w_pct` large | Stock has its own driver; sector framing is less informative |
| `beta_60d > 1.2` and bullish momentum | High-beta name in an uptrend — leveraged exposure to sector tailwinds |
| `beta_60d < 0.5` and `weekly_return_pct` large | Move was largely idiosyncratic, not a sector beta call |

---

## Report Sections

Each report MUST contain all 8 sections in this order.

### 1. Executive Summary

One concise paragraph (4–6 sentences) covering:
- the week's overall direction (up / down / flat) using `weekly_return_pct`
- **how it compared to the sector** using `relative_return_1w_pct` (this is
  the lead for a weekly report; do not skip it)
- the single most material development of the week
- whether technical momentum confirms or contradicts the price action

### 2. Investment Rating & Thesis
**Rating: {RATING}**

**The Market Debate:**
- **Bulls are focusing on:** {1 sentence on the key bullish driver}
- **Bears are concerned about:** {1 sentence on the key bearish risk}

**Core Logic:**
- {thesis bullet 1, with cited metric / news / filing}
- {thesis bullet 2}
- {thesis bullet 3, ideally referencing relative-to-sector context}

State the rating using exactly one of:

- `STRONG_BUY`, `BUY`, `HOLD`, `SELL`, `STRONG_SELL`

**Rating philosophy.** The rating is your call to the reader — the part of
the report that converts a week's evidence into a usable signal. Make the
call your overall analysis supports: when the alpha, momentum, beta, and
news blocks point the same way, the rating should reflect that, not retreat
to a neutral default. A HOLD on a week whose prose already reads clearly
bearish (or bullish) is a low-value outcome — the reader could already see
the lean from the text and is looking to you for the call. Reserve HOLD for
weeks where the evidence is **genuinely mixed**: bull and bear cases of
similar weight, conflicting signals across blocks, or no near-term catalyst
to break the tie. You don't need to manufacture conviction you don't have,
but you also shouldn't understate the conviction you do have. Pick the
rating that an investor reading only the rating would benefit from.

Then provide **2–3 distinct, evidence-based thesis bullets**, each grounded
in a specific metric, news item, or filing passage. For a weekly report, at
least one bullet should explicitly reference the **relative-to-sector**
context (e.g., *"AAPL outperformed the mega-cap basket by 2.7pp this week
despite a market-wide sell-off"*) — unless the symbol has no benchmark.

*Note: Keep thesis bullets focused on the high-level logic and impact. Do NOT redundantly summarize all news events here; leave the detailed event breakdowns for Section 4.*

### 3. Weekly Price Performance & Technical Indicators

Present all 16 metrics — plus the four context fields (`support_20d`,
`resistance_20d`, `volume_ratio`, `cmf_20day`) for technical synthesis —
in a structured Markdown table. See template below.

If the symbol's `benchmark_basket` is empty, write `N/A (no peers in
current dataset)` for all 5 beta-block rows.

**Important:** Immediately below the table, write a brief "Technical Synthesis" paragraph (2–3 sentences). Interpret the relationship between volume, moving averages, and momentum. Note any obvious support/resistance levels or volume divergences (e.g., high-volume selling vs low-volume drift).

### 4. News & Catalysts

First, assign a **Sentiment Thermometer** tag (one of: Euphoric, Cautiously Optimistic, Mixed, Defensive, Panic) summarizing the overall news tone for the week.

Then, summarize the **3–5 most relevant news items** from the `list_news` scan.
For each:
- date and one-line summary
- why it matters for the thesis (positive / negative / neutral)

If the digest is empty: *"No material news for [SYMBOL] during the week ending [DATE]."*

### 5. Earnings & Filings Update

If `list_filings` returned at least one row and you read the latest
filing's MD&A and Risk Factors via `get_filing_section`:
- Name the filing (e.g., "10-Q filed 2024-10-23")
- Summarize the MD&A in 2–3 sentences
- Summarize the most relevant Risk Factor in 1–2 sentences
- *CRITICAL:* Acknowledge the filing date. If the filing is weeks or months old, explicitly frame it as the underlying structural fundamental background, NOT as a new catalyst for this week.

If `list_filings` returned empty: *"No 10-K or 10-Q documents are available on or before [DATE]."*

### 6. Sector & Relative Performance

This is the new section that distinguishes a weekly from a daily report.
Comment on:

- **Where the stock is in its sector this week** (`relative_return_1w_pct`)
- **The medium-term relative trend** (`relative_return_4w_pct`)
- **How sector-driven the stock is** (`correlation_60d`, `beta_60d`)
- **Sector framing using `list_peers` output** — name the peers explicitly

If `benchmark_basket` is empty, write a short paragraph stating: *"No peer
benchmark is available in the current dataset for [SYMBOL]; sector
relative performance cannot be computed. The remaining analysis relies on
the absolute-return metrics in Section 3."*

### 7. Risk Factors & Uncertainties

List **2–3 specific, evidence-based risks** grounded in:
- a metric (e.g., *"weekly_volatility of 4.2% is elevated"*),
- a news item, or
- a filing passage.

Avoid boilerplate. If you cite macro risk, tie it to an observed metric or
news item.

**CRITICAL RULE ON MISSING CONTEXT:** If the stock experienced a major price move (e.g., a massive drop or surge) but `list_news` or other tools returned NO apparent reason, **do NOT invent or hallucinate a catalyst.** You must explicitly state in this section that the catalyst for the recent move is unclear based on available data.

### 8. Recommendation, Outlook & Scenarios

Restate the rating and explain **what to monitor in the upcoming week**:
- specific catalysts (earnings, product launches, analyst events)
- specific technical levels (e.g., "watch the 20-day MA at $238.42")

Then, clearly outline the Bull/Bear scenarios (Upside/Downside Triggers):
- **Upside Trigger / Bull Case:** What specific event, metric shift, or news would cause a rating upgrade?
- **Downside Trigger / Bear Case:** What specific event, metric shift, or news would cause a rating downgrade or thesis breakdown (e.g., "if `relative_return_1w_pct` flips negative for two consecutive weeks")?

Do NOT predict specific price targets.

---

## Output Writing

Use the bundled write helper.

```bash
python3 .claude/skills/report_generation/scripts/upsert_report.py \
    --symbol TSLA \
    --target-date 2025-03-07 \
    --action BUY \
    --price 238.03 \
    --model claude-sonnet-4-6 \
    --output-root <whatever the caller specified, e.g. /io/slot1> \
    <<'REPORT'
# Weekly Equity Research Report: TSLA
...full markdown body...
REPORT
```

| Flag            | Meaning |
|-----------------|---|
| `--symbol`      | Ticker symbol |
| `--target-date` | Week-ending date in `YYYY-MM-DD` |
| `--action`      | One of the five allowed rating tokens |
| `--price`       | **Required.** The week-close price — pass `week_close` from the `get_weekly_metrics` dict you pulled in step 2. Omitting it leaves the field as the sentinel `0.0`, which downstream consumers will treat as missing data. |
| `--model`       | Actual model identifier used in filenames |
| `--output-root` | **Pass the value the caller specified in the invocation** (e.g. `/io/slot1`). Falls back to `results/report_generation` (relative to cwd) only if no value was given — that default is rarely writable inside a sandbox, so omitting it usually causes a `PermissionError`. |

Files written:

```
{output_root}/report_generation_{symbol}_{model}.json
{output_root}/report_generation_{symbol}_{model}/report_generation_{symbol}_{YYYYMMDD}_{model}.md
```

Calling the script with the same `--target-date` overwrites that week's
entry.

---

## Output Format

Use this Markdown template exactly. Replace `{...}` with computed values.

````markdown
# Weekly Equity Research Report: {TICKER}

**Model:** {model} | **Week Ending:** {TARGET_DATE}
**Rating:** {RATING}

---

### 1. Executive Summary
{4–6 sentence paragraph; lead with stock's move AND its move vs sector}

---

### 2. Investment Rating & Thesis
**Rating: {RATING}**

**The Market Debate:**
- **Bulls are focusing on:** {1 sentence on the key bullish driver}
- **Bears are concerned about:** {1 sentence on the key bearish risk}

**Core Logic:**
- {thesis bullet 1, with cited metric / news / filing}
- {thesis bullet 2}
- {thesis bullet 3, ideally referencing relative-to-sector context}


---

### 3. Weekly Price Performance & Technical Indicators

| Metric | Value |
|---|---|
| **Alpha block** | |
| Week Open | {week_open} |
| Week Close | {week_close} |
| Weekly Return | {weekly_return_pct}% |
| 4-Week Return | {return_4week_pct}% |
| 20-Day MA | {ma_20day} |
| Price vs 20-Day MA | {price_vs_ma20} |
| Weekly Volatility | {weekly_volatility}% |
| Distance from 52-Week High | {dist_from_52w_high_pct}% |
| 20-Day Support | {support_20d} |
| 20-Day Resistance | {resistance_20d} |
| **Momentum & Volume block** | |
| Short-term Momentum | {momentum_short} |
| MACD Signal | {macd_signal} |
| RSI (14) | {rsi_14} ({rsi_class}) |
| Volume Ratio (vs 20d avg) | {volume_ratio} |
| Chaikin Money Flow (20d) | {cmf_20day} |
| **Beta block (vs equal-weighted sector basket)** | |
| Sector Basket Return (1W) | {sector_basket_return_1w_pct}% |
| Relative Return (1W) | {relative_return_1w_pct}% |
| Relative Return (4W) | {relative_return_4w_pct}% |
| Correlation (60D) | {correlation_60d} |
| Beta (60D) | {beta_60d} |

**Technical Synthesis:**
{2-3 sentences analyzing support/resistance, moving average trends, and volume confirmation/divergence.}

---

### 4. News & Catalysts

**Sentiment Thermometer:** [ Euphoric | Cautiously Optimistic | Mixed | Defensive | Panic ]

- **{date}** — {headline summary}. {why it matters}.
- **{date}** — {headline summary}. {why it matters}.
- **{date}** — {headline summary}. {why it matters}.

---

### 5. Earnings & Filings Update
{Summary referencing the filing date and document_type, plus 2–3 sentences
on MD&A and 1–2 sentences on Risk Factors. Or "No new 10-K or 10-Q on or
before {DATE}."}

---

### 6. Sector & Relative Performance
{Paragraph covering relative_return_1w_pct, relative_return_4w_pct,
correlation_60d, beta_60d. Name the peers from list_peers explicitly.
If benchmark_basket is empty, state the limitation.}

---

### 7. Risk Factors
- {risk 1 with cited evidence}
- {risk 2 with cited evidence}

---

### 8. Recommendation, Outlook & Scenarios
{Restate rating. What to monitor next week: catalysts, technical levels,
relative-strength triggers.}
````

---

## Worked Example: Reading the Metrics

Suppose `get_weekly_metrics("AAPL", "2025-03-07")` returns:

```json
{
  "week_open": 241.79, "week_close": 238.03,
  "weekly_return_pct": -1.15, "return_4week_pct": 2.62,
  "ma_20day": 238.42, "price_vs_ma20": "below",
  "weekly_volatility": 2.34, "dist_from_52w_high_pct": -8.48,

  "momentum_short": "down", "macd_signal": "bearish_weakening",
  "rsi_14": 50.8, "rsi_class": "neutral",

  "sector_basket_return_1w_pct": -3.85,
  "relative_return_1w_pct": 2.70,
  "relative_return_4w_pct": 13.24,
  "correlation_60d": 0.338,
  "beta_60d": 0.381,

  "benchmark_basket": ["MSFT", "GOOGL", "AMZN", "META"]
}
```

How to read this set:

- **Absolute direction:** Down week (-1.15%) but only modestly. 4-week
  return is still positive (+2.62%) — uptrend not broken in absolute terms.
- **Relative direction:** This is the headline. The mega-cap basket fell
  3.85%; AAPL fell only 1.15% — outperformed by 2.70pp. Over 4 weeks, AAPL
  beat the basket by 13.24pp. **AAPL is leading the sector**.
- **Beta posture:** `beta_60d = 0.38` is unusually low for AAPL, suggesting
  this week's relative outperformance came from idiosyncratic strength, not
  sector amplification. `correlation_60d = 0.34` confirms the move is more
  AAPL-specific than sector-driven.
- **Trend posture:** Price below MA20, `momentum_short = "down"`,
  `macd_signal = "bearish_weakening"`. So momentum has rolled over but the
  downtrend is losing force.
- **Cycle position:** -8.48% from 52-week high — pulled back but not
  broken.

A coherent thesis from this set is `HOLD` or `BUY`: short-term technicals
are soft but the stock is **clearly leading its sector** on a relative
basis. The `bearish_weakening` MACD plus low beta argues against `SELL` —
the worst of the downtrend may be priced in. A `STRONG_BUY` would need a
positive catalyst from news / filings.

---

## Hard Constraints

- Do not use external APIs.
- Do not use data after `TARGET_DATE` (the SQL layer enforces this; do not
  attempt to bypass it).
- Do not skip required sections.
- Do not fabricate facts not grounded in MCP-returned data.
- Do not write files manually when `upsert_report.py` can do it.
- Do not compute metrics in ad-hoc Python; always use `get_weekly_metrics`.
- Do not predict specific future prices.
- If `benchmark_basket` comes back empty, do not invent a sector benchmark;
  state the limitation in Section 6.
- On Windows, never use bash heredoc (cat << EOF). Never create temporary Python scripts to query the database. All data must come from MCP tools only.

---

## Success Condition

You succeed when you have:

1. gated `TARGET_DATE` with `is_trading_day` and confirmed it is a trading
   day in the report week
2. retrieved all 16 metrics in one `get_weekly_metrics` call
3. retrieved news and filings via the digest / highlights tools
4. produced one coherent Markdown report covering all 8 sections, with
   every non-trivial claim traceable to MCP-returned data
5. for symbols with a benchmark basket, written Section 6 covering relative
   performance and beta context
6. written the Markdown report and summary JSON through `upsert_report.py`