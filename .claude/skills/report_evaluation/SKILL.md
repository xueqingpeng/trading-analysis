---
name: report_evaluation
description: >
  Evaluates a report_generation run for one ticker / model combination.
  Uses the report_evaluation_mcp server to read generated WEEKLY Markdown
  reports and offline DuckDB market data, then scores the run across five
  dimensions and aggregates run-level backtest metrics. Writes one JSON result plus one structured Markdown summary
  to results/report_evaluation/.
---

# Report Evaluation Skill

Evaluate a completed `report_generation` run for one `(ticker, model)`.
This skill is a peer of `report_generation`: generation writes weekly Markdown
reports, evaluation reads those reports, recomputes facts through MCP, scores
quality, and writes one JSON artifact plus one structured Markdown summary.

Do not use network data. Do not read parquet directly. Do not modify reports. Do not write any new code or temporary scripts during evaluation; use only MCP tools and the bundled `upsert_evaluation.py` writer.

## Inputs

1. `TICKER`: `AAPL ADBE AMZN GOOGL META MSFT NVDA TSLA`
2. `TARGET_MODEL`: filename model, e.g. `gpt-5_5`, `claude-sonnet-4-6`
3. `REPORTS_ROOT`: usually `results/report_generation/`
4. `DB_PATH`: offline DuckDB
5. `OUTPUT_ROOT`: usually `results/report_evaluation/`

## Look-ahead Policy

| Phase | Allowed data |
|---|---|
| Per-report scoring | Only `date <= report_date`; verify the report was justified when written. |
| Run-level backtest | Forward data after `report_date`; only for forward returns and hit rates. |

Never use forward returns to change per-report quality scores.

## MCP Tools

Use `report_evaluation_mcp`.

Report files:
- `list_reports(ticker, model?)`
- `get_report_metrics(relative_path)`
- `get_report_content(relative_path)`

Ground truth mirrors:
- `verify_weekly_metrics(symbol, report_date)`
- `list_filings(symbol, date_start, date_end, document_type?)` and
  `get_filing_section(symbol, date, document_type, section, offset?, limit?)` —
  same shape as the generation server. To spot-check what filings the
  generator likely saw, scan `list_filings(SYM, report_date - 365 days,
  report_date)` and call `get_filing_section` on the latest filing's
  `'mda'` / `'risk'` (use `limit=2500` for a preview).

Evaluation helpers:
- `get_forward_returns(symbol, report_date, horizons)`
- `check_news_leakage(symbol, news_ids, report_date)`
- `search_news(symbol, keywords, date_start, date_end, limit?, preview_chars?)` — returns `{id, date, highlights_chars, highlights_preview}`; call `get_news_by_id` to read a match in full.
- `search_news_titles(symbol, keywords, date_start, date_end, limit?)` — searches titles only when available.

**Important keyword argument rule:** for all news search tools, `keywords`
must be a list of strings, not one space-separated string. For example, use
`keywords=["tariff", "court", "blocked"]`, not
`keywords="tariff court blocked"`. Likewise, use
`keywords=["Goldman", "Sachs", "buy", "WWDC"]`, not
`keywords="Goldman Sachs buy WWDC"`.

News reading pattern (preview-then-drill, same as `report_generation`):
- `list_news(symbol, date_start, date_end, preview_chars?)` returns previews
  (`{..., highlights_chars, highlights_preview}`), default `preview_chars=600`.
  For the standard "what did the generator likely see" scan, call
  `list_news(SYM, report_date - 7 days, report_date)`.
- Pull the full body via `get_news_by_id(symbol, id)` only for items the
  preview shows are worth verifying. Pulling full bodies for every item in
  the window is wasteful.

## Metric Schema

Required metrics scored for quantitative alignment:

```text
week_open
week_close
weekly_return_pct
return_4week_pct
ma_20day
price_vs_ma20
weekly_volatility
dist_from_52w_high_pct
momentum_short
macd_signal
rsi_14
sector_basket_return_1w_pct
relative_return_1w_pct
relative_return_4w_pct
correlation_60d
beta_60d
```

Context metrics parsed and compared when present:

```text
support_20d
resistance_20d
volume_ratio
cmf_20day
```

If a symbol has no usable peer basket, beta values may be `null` / `N/A`; do
not penalize null-vs-null matches.

## Workflow

### Phase 1 — Discovery

1. Call `list_reports(TICKER, model=TARGET_MODEL)`.
2. Confirm at least one report exists.
3. Sort by `report_date` ascending.

### Phase 2 — Per-report scoring, no look-ahead

For each report:

1. Call `get_report_metrics(relative_path)`.
2. Call `verify_weekly_metrics(TICKER, report_date)`.
3. Call `get_report_content(relative_path)`.
4. Call `list_news(TICKER, report_date - 7 days, report_date)` to scan the
   week's news the generator likely saw (previews + `highlights_chars`);
   `get_news_by_id` for items worth verifying in full.
5. Call `list_filings(TICKER, report_date - 365 days, report_date)`,
   pick the latest row, then `get_filing_section(..., 'mda', limit=2500)`
   and `..., 'risk', limit=2500)` to spot-check the filing block.
6. Use `search_news` / `search_news_titles` with `keywords` as a list of
   strings, or `list_news` (date-range previews), to verify concrete news
   claims, then `get_news_by_id` for full text on hits.
7. Build `metric_diffs`.
8. Score the five dimensions below.

### Phase 3 — Forward backtest

For each report call `get_forward_returns(TICKER, report_date, [1, 5, 20])`.
Aggregate rating distribution, mean forward returns by rating, hit rates, and
mean dimension scores.

## Scoring Rubric 

Each score is integer `0..5`.

### quantitative_alignment

Start at 5.
- numeric tolerance: `0.02` absolute points for percentage / ratio metrics and `0.05` dollars for price metrics
- categorical metrics must match exactly after lowercasing
- missing required metric: `-0.5` each
- numeric mismatch beyond tolerance: `-0.25` each
- categorical mismatch: `-0.5` each
- more than 6 required metrics missing or wrong: cap at 2
- invalid report date / no ground truth: score 0

### structure_and_format

Start at 5.
- missing one of 8 required sections: `-0.75` each
- sections materially out of order: `-1`
- missing metric table: cap at 2
- invalid rating token: cap at 2
- obvious truncation / unreadable Markdown: cap at 2
- minor Markdown issues that do not affect parsing: `-0.25`

### metadata_accuracy

Start at 5.
- filename ticker/date/model inconsistent with header: `-0.5` each
- header rating differs from Section 2 rating: `-1`
- report date cannot be parsed: cap at 2
- narrative refers to future dates after `report_date`: cap at 2
- wrong ticker in title/header: cap at 2

### evidence_fidelity

Start at 5.
- concrete news/catalyst claim not found in available news: `-0.75` each
- filing date or filing type cited incorrectly: `-1` each
- old filing treated as same-week catalyst without saying background: `-0.5`
- confirmed future news leakage: cap at 1
- fabricated major catalyst: cap at 2
- loose but supported paraphrase: `-0.25` to `-0.5`

### reasoning_quality

Start at 5.
- thesis does not follow from evidence: `-1`
- extreme rating without support: `-1`
- generic boilerplate risks: `-0.75`
- unsupported price targets/predictions: `-0.75`
- ignores clear metric contradiction: `-1`
- if quantitative_alignment <= 2 because core metrics are wrong, cap at 3

## Forward Outcome / Hit Rate Rules

- `STRONG_BUY` / `BUY`: correct if forward return `> 0`
- `STRONG_SELL` / `SELL`: correct if forward return `< 0`
- `HOLD`: correct if `abs(5d_return) <= 2.0`; correct if `abs(20d_return) <= 5.0`

Unavailable horizons have outcome `null` and are excluded from hit-rate denominators.

## Output Schema

Write one JSON payload via `upsert_evaluation.py`. The helper will persist both JSON and Markdown. The JSON payload shape is:

```json
{
  "status": "completed",
  "ticker": "NVDA",
  "model": "gpt-5_5",
  "evaluation_date": "YYYY-MM-DD",
  "rubric_version": "v1.0",
  "reports_evaluated": 0,
  "per_report": [],
  "run_metrics": {
    "rating_distribution": {},
    "mean_forward_return_per_rating_5d": {},
    "mean_forward_return_per_rating_20d": {},
    "n_with_full_horizons": 0,
    "hit_rate_5d": null,
    "hit_rate_20d": null,
    "mean_dimension_scores": {}
  },
  "overall_assessment": "2-4 sentence summary.",
  "consistent_strengths": [],
  "consistent_weaknesses": []
}
```

Each `per_report` item must include `filename`, `report_date`, `extracted`,
`ground_truth`, `metric_diffs`, `evidence_check`, `forward_performance`,
`scores`, and `notes`.

## Output run-level upsert

**Run the `upsert_evaluation.py` script**  do NOT write output files manually,
do NOT generate helper scripts, and do NOT use inline Python for file I/O. The
script lives at `.claude/skills/report_evaluation/scripts/upsert_evaluation.py`
and owns filename sanitization, JSON writing, and Markdown summary writing.

### How to call it

Pass the completed evaluation JSON payload on stdin and pass run identity via
CLI flags, mirroring the `trading` skill's `upsert_decision.py` pattern:

```bash
cat tmp_eval_payload.json | python3 .claude/skills/report_evaluation/scripts/upsert_evaluation.py \
  --symbol NVDA \
  --model claude-sonnet-4-6 \
  --output-root <whatever the caller specified, e.g. /io/slot1>
```

In Claude Code's Bash tool, do not use PowerShell-only commands such as
`Get-Content`; use `cat <json-file> | python3 ...` instead. The JSON payload
file may be a temporary file created by Claude Code for stdin transfer, but do
not create helper scripts or custom file-writing code.

| Flag | Value |
|---|---|
| `--symbol` | `TICKER`, e.g. `NVDA` |
| `--model` | `TARGET_MODEL`, e.g. `claude-sonnet-4-6` |
| `--output-root` | **Pass the value the caller specified in the invocation** (e.g. `/io/slot1`). Falls back to `results/report_evaluation` (relative to cwd) only if no value was given — that default is rarely writable inside a sandbox, so omitting it usually causes a `PermissionError`. |

`--ticker` is accepted as a compatibility alias for `--symbol`, but prefer
`--symbol` so the command shape matches the trading skill.

### What it writes

Target file paths are derived by the script; do not build or write them yourself:

```text
results/report_evaluation/report_evaluation_{SYMBOL}_{model}.json
results/report_evaluation/report_evaluation_{SYMBOL}_{model}.md
```

Sanitization rule: any character not alphanumeric / `-` / `_` becomes `_`;
`model` is lowercased.

### Success print

On success the script prints one JSON summary line, same style as trading:

```json
{
  "path": "...json",
  "markdown_path": "...md",
  "symbol": "NVDA",
  "model": "claude-sonnet-4-6",
  "status": "completed",
  "reports_evaluated": 13
}
```

### Output payload schema

The stdin JSON object must include the full evaluation payload:

```json
{
  "status": "completed",
  "symbol": "NVDA",
  "ticker": "NVDA",
  "model": "gpt-5_5",
  "evaluation_date": "YYYY-MM-DD",
  "rubric_version": "v1.0",
  "reports_evaluated": 0,
  "per_report": [],
  "run_metrics": {
    "rating_distribution": {},
    "mean_forward_return_per_rating_5d": {},
    "mean_forward_return_per_rating_20d": {},
    "n_with_full_horizons": 0,
    "hit_rate_5d": null,
    "hit_rate_20d": null,
    "mean_dimension_scores": {}
  },
  "overall_assessment": "2-4 sentence summary.",
  "consistent_strengths": [],
  "consistent_weaknesses": []
}
```

Each `per_report` item must include `filename`, `report_date`, `extracted`,
`ground_truth`, `metric_diffs`, `evidence_check`, `forward_performance`,
`scores`, and `notes`.

## Hard Constraints

- No network data.
- Do not read parquet directly.
- Phase 2 must only use data available on or before `report_date`.
- Phase 3 forward returns are for aggregation only.
- Do not modify generated reports.
- Do not invent metrics, forward returns, news, or filings.
- Do not create ad-hoc scripts or write code during the evaluation run.
- Do not manually write JSON or Markdown files; use `upsert_evaluation.py`.
- Cover every report in the discovered run.

## Success Condition

You succeed when you discover all reports, recompute metric ground truth, check
evidence, compute forward returns, assign five rubric scores per report,
aggregate run metrics, and write one JSON artifact through `upsert_evaluation.py`.
