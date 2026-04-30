---
name: report_evaluation
description: >
  Evaluates a report_generation run for one ticker / agent / model combination.
  Uses the report_evaluation_mcp server to read generated WEEKLY Markdown
  reports and offline DuckDB market data, then scores the run across five
  dimensions and aggregates run-level backtest metrics. Writes one JSON
  result to results/report_evaluation/.
---

# Report Evaluation Skill (V1 — scaffold)

> **Status:** V1 scaffold. The tooling layer and three-phase workflow are
> complete and stable. The numeric thresholds inside the per-dimension rubric
> (Section "Scoring Rubric") are intentionally left as **`TODO: calibrate
> from samples`** — they will be filled in after we generate ~15–20 sample
> weekly reports with the latest `report_generation` skill and observe what
> kinds of errors actually occur. Do **not** invent thresholds before that
> calibration. If you are an agent reading this skill before calibration,
> use the structural workflow but mark every numeric score as
> `null` with a `notes` field describing what you saw.

You are evaluating a set of weekly equity research reports produced by the
`report_generation` skill for one `(agent, ticker, model)` run.

Your job is to:

1. read the generated report files,
2. compare them against offline market / news / filings data through MCP,
3. score the run across five dimensions,
4. compute run-level backtest metrics (forward returns, hit rate, etc.), and
5. write one final JSON artifact.

Everything you know about the market must come from the MCP tools described
below. Do not call external APIs. Do not use network data. Do not read
parquet directly.

---

## Inputs

The user invocation specifies or implies:

1. `TICKER` — one of `AAPL ADBE AMZN BMRN CRM GOOGL META MSFT NVDA TSLA`
2. `TARGET_AGENT` — agent name embedded in filenames, e.g. `codex`,
   `claude-code`
3. `TARGET_MODEL` — model name embedded in filenames, e.g. `gpt-5`,
   `claude-sonnet-4-6`
4. `REPORTS_ROOT` — either:
   - the run-specific folder
     `results/report_generation/{agent}_report_generation_{ticker}_{model}/`
   - or the parent folder `results/report_generation/`
5. `DB_PATH` — path to the offline DuckDB file used by MCP
6. `OUTPUT_ROOT` — directory where the final evaluation JSON is written

Typical prompt shape:

```
Evaluate the codex/NVDA/gpt-5 run.
Reports parent: /path/to/results/report_generation/
DuckDB: /path/to/data/trading_env.duckdb
Output: /path/to/results/report_evaluation/
```

---

## Look-ahead Policy (Important)

This is **the opposite of `report_generation`**.

| Phase | What you may read |
|---|---|
| **Per-report scoring** | Only data with `date <= report_date`. Use this to verify the report's claims could have been justified at write time. |
| **Run-level backtest** | Data with `date > report_date` is REQUIRED to compute forward returns. |

The MCP layer does NOT enforce a hard cap on look-ahead (unlike
`report_generation_mcp`). The boundary is enforced by the workflow below:
which tools you call when, and which date ranges you pass.

---

## Data Access

Use tools on the `report_evaluation_mcp` server.

### Report file tools

| Tool | When to use |
|---|---|
| `list_reports(ticker, agent?, model?)` | Phase 1: discover all reports in the run. |
| `get_report_metrics(relative_path)` | Phase 2: structured extraction of rating + 17 metrics from the Markdown table. **Prefer this over raw markdown parsing.** |
| `get_report_content(relative_path)` | Phase 2: raw markdown when you need to read narrative sections (thesis, risks, summary). |

### Generation mirrors (recompute what the generator computed)

| Tool | When to use |
|---|---|
| `verify_weekly_metrics(symbol, report_date)` | Phase 2: compute the canonical 17 ground-truth metrics. **This is the input to quantitative_alignment scoring.** |
| `get_news_digest_mirror(symbol, target_date, lookback_days?, top_k?)` | Phase 2: see what news context the generator likely had. |
| `get_filing_highlights_mirror(symbol, target_date, document_type?, max_chars?)` | Phase 2: see what filing context the generator likely had. |

### Evaluation-specific tools

| Tool | When to use |
|---|---|
| `get_forward_returns(symbol, report_date, horizons)` | **Phase 3:** compute 1d/5d/20d forward returns for backtest aggregation. |
| `check_news_leakage(symbol, news_ids, report_date)` | Phase 2: verify any news_id the report cited is dated `<= report_date`. |
| `search_news_titles(symbol, keywords, date_start, date_end, limit?)` | Phase 2: verify a claimed event actually appears in the news in a given window. |

### Raw market / news / filings (fallback)

Same shape as on the generation server; use these only when the structured
tools above are insufficient.

| Tool | Notes |
|---|---|
| `get_prices(symbol, date_start, date_end)` | OHLCV rows. |
| `list_news(symbol, date_start, date_end)` | Lightweight metadata; no `highlights`. |
| `get_news_by_id(symbol, id)` | Full article body. |
| `list_filings(symbol, date_start, date_end, document_type?)` | Filings metadata. |
| `get_filing_section(symbol, date, document_type, section, offset, limit)` | Filing text. |
| `get_indicator(symbol, date_start, date_end, indicator, length?)` | Custom indicator series. |

### Rules

- Never invent metric values; always recompute via `verify_weekly_metrics`.
- Never invent forward returns; always compute via `get_forward_returns`.
- Never claim a news event existed without verifying via `search_news_titles`
  or `list_news`.
- Do not write intermediate scripts to disk. Inline calculations on
  tool-returned data are fine.

---

## Three-Phase Workflow

This skill runs in three explicit phases. Phase 2 must respect
`<= report_date`; Phase 3 deliberately uses forward data.

### Phase 1 — Discovery

1. Call `list_reports(TICKER, agent=TARGET_AGENT, model=TARGET_MODEL)`.
2. Confirm the returned list is non-empty and matches the expected run.
3. Sort by `report_date` ascending.

If the list is empty, abort and report to the user that no matching files
were found.

### Phase 2 — Per-report scoring (look-back only)

For each report file in chronological order:

1. **Extract the report's claims.**
   - Call `get_report_metrics(relative_path)` to pull rating and 17 metrics.
   - Call `get_report_content(relative_path)` if you need to read the
     narrative sections (Section 1, 2, 4, 5, 6, 7, 8 prose).

2. **Compute ground truth (using only `<= report_date`).**
   - Call `verify_weekly_metrics(SYMBOL, report_date)` for the 17 reference
     values.
   - Call `get_news_digest_mirror(SYMBOL, report_date)` to see what news
     was available at write time.
   - Call `get_filing_highlights_mirror(SYMBOL, report_date)` to see what
     filing was the most recent on or before `report_date`.

3. **Verify evidence claims.**
   - If the report cites specific news ids, call `check_news_leakage`.
   - For thesis claims that mention specific themes (e.g., "regulatory
     scrutiny", "AI rollout"), call `search_news_titles` with relevant
     keywords to confirm the theme actually appeared in the available
     news window.

4. **Score the report on five dimensions** (see "Scoring Rubric" below).

5. **Record diffs explicitly** in the per-report output:
   - `report_metrics` — what the report stated
   - `ground_truth_metrics` — what the MCP recomputed
   - `metric_diffs` — per-metric difference

### Phase 3 — Run-level backtest (forward look ALLOWED here)

Aggregate across all reports:

1. For each report, call `get_forward_returns(SYMBOL, report_date,
   [1, 5, 20])`.
2. Compute, across the run:
   - rating distribution (`STRONG_BUY` / `BUY` / `HOLD` / `SELL` /
     `STRONG_SELL` counts)
   - mean forward return per rating bucket at each horizon
   - hit rate per horizon (see rubric for the "hit" definition — currently
     TODO)
   - mean of the five dimension scores

Write a single JSON artifact via `upsert_evaluation.py`.

---

## Scoring Rubric

> **All numeric thresholds in this section are `TODO: calibrate from
> samples`.** The methodology is fixed; the cut-points are not. Until
> calibration is done, agents should record observations in the `notes`
> field rather than emit numeric scores. After calibration, replace each
> TODO with a concrete cut-point.

Score each report on a `0–5` integer scale across five dimensions.

### 1. `quantitative_alignment`

**What it measures:** Do the 17 reported metric values match the ground
truth recomputed from MCP, and does the rating direction agree with the
observed price action?

**Method:**
- Compare each of the 17 metrics in `report_metrics` vs
  `ground_truth_metrics`.
- For numeric metrics, compute relative error
  `abs(reported - truth) / abs(truth)` (or absolute error in % points for
  metrics that are already in %).
- For categorical metrics (`price_vs_ma20`, `momentum_short`,
  `macd_signal`, plus the implicit `rsi_class`), check exact match.

**Rubric:** TODO calibrate from samples. Open questions to resolve at
calibration time:
- `TODO`: tolerance for numeric metrics — is `±0.01%` strict equality, or
  is `±1%` relative error acceptable?
- `TODO`: do all 17 metrics weight equally, or are categorical (3) heavier
  because they are direct inputs to the thesis?
- `TODO`: how many metric mismatches push 5 → 4 → 3 → 2 → 1 → 0?
- `TODO`: does an obvious rating-vs-price-direction contradiction (e.g.,
  `STRONG_BUY` issued in a `weekly_return_pct < -10%` week with
  `momentum_short=down` and `macd_signal=bearish_strengthening`)
  immediately cap the score?

### 2. `structure_and_format`

**What it measures:** Is the report well-formed?

**Method:**
- Confirm all 8 required sections are present and in order.
- Confirm the metric table renders all 17 rows.
- Confirm rating uses one of the 5 allowed tokens.
- Confirm no obvious truncation or markdown corruption.

**Rubric:** TODO calibrate from samples. Open questions:
- `TODO`: does a missing section automatically deduct one full point each,
  or does total absence of a section cap the score at 2?
- `TODO`: how strict on minor markdown errors (e.g., misaligned table
  pipes) — counted as zero impact or as -1?

### 3. `metadata_accuracy`

**What it measures:** Is the report's metadata internally consistent?

**Method:**
- Filename → header consistency: agent, model, ticker, week-ending date.
- Rating cited in Section 2 matches rating in header.
- All dates referenced are valid and `<= report_date`.

**Rubric:** TODO calibrate from samples. Open questions:
- `TODO`: which inconsistencies are immediate -1, -2, or score-cap?
- `TODO`: is a future-dated reference (look-ahead leak in narrative) an
  immediate score-cap, or is it counted under `evidence_fidelity`?

### 4. `evidence_fidelity`

**What it measures:** Are the report's claims about news / filings /
catalysts grounded in actual offline evidence available at `report_date`?

**Method:**
- For every concrete claim in the News & Catalysts section, check it is
  traceable via `get_news_digest_mirror` or `search_news_titles`.
- For every filing reference (Section 5), check the cited
  `(filing_date, document_type)` exists via `list_filings` and is
  `<= report_date`.
- Run `check_news_leakage` on any explicit news ids cited.
- Flag any "future-dated event" mentioned in narrative.

**Rubric:** TODO calibrate from samples. Open questions:
- `TODO`: distinguish "fabricated" (no DB evidence) vs "loose paraphrase"
  (theme exists in DB but specifics differ) — same penalty or different?
- `TODO`: how to score narrative cites without specific news ids — count
  unverifiable claims as half-fidelity?
- `TODO`: does any single confirmed leak (`is_leak=true`) cap the score
  at 2?

### 5. `reasoning_quality`

**What it measures:** Is the thesis coherent and consistent with the
evidence presented?

**Method:** This dimension is partly LLM-judged. Read Sections 2, 7, 8.
Ask:
- Does the rating follow from the evidence stated in the thesis bullets?
- Are the risks acknowledged and tied to actual data?
- Does the outlook in Section 8 follow logically without overreach?

**Rubric:** TODO calibrate from samples. Open questions:
- `TODO`: how to weight "internally consistent but wrong" vs "directionally
  right but weakly argued"?
- `TODO`: should `reasoning_quality` be capped if `quantitative_alignment`
  shows a rating-vs-price contradiction?

---

## Backtest Aggregation (Phase 3)

These are **mechanical aggregations** — no rubric needed.

For each `(report_date, rating)` in the run:
1. Pull `get_forward_returns(SYMBOL, report_date, [1, 5, 20])`.
2. Categorize:
   - `rating_outcome_5d`: TODO calibrate. Initial proposal pending sample
     review:
     - For BUY / STRONG_BUY: outcome = `correct` if `5d return > 0`,
       else `incorrect`.
     - For SELL / STRONG_SELL: outcome = `correct` if `5d return < 0`,
       else `incorrect`.
     - For HOLD: outcome = `correct` if `|5d return| < threshold` (TODO
       threshold), else `drifted_up` / `drifted_down`.

The threshold for HOLD's "stayed flat" is exactly the kind of cut-point we
will set after looking at the empirical 5d-return distribution across the
sample reports.

Run-level aggregations to compute regardless:
- `rating_distribution`: count by token
- `mean_forward_return_per_rating_5d`: mean 5d return per rating bucket
- `mean_forward_return_per_rating_20d`: mean 20d return per rating bucket
- `n_with_full_horizons`: number of reports for which all horizons were
  available

Once the rating-outcome thresholds are calibrated:
- `hit_rate_5d`, `hit_rate_20d`

---

## Output Schema

Write one JSON file:

```
{output_root}/{agent}_report_evaluation_{ticker}_{model}.json
```

Schema:

```json
{
  "status": "completed",
  "agent": "codex",
  "ticker": "NVDA",
  "model": "gpt-5",
  "evaluation_date": "2026-04-24",
  "rubric_version": "v1-scaffold",
  "reports_evaluated": 13,

  "per_report": [
    {
      "filename": "codex_report_generation_NVDA_20250307_gpt-5.md",
      "report_date": "2025-03-07",

      "extracted": {
        "rating": "BUY",
        "rating_numeric": 1,
        "report_metrics": { /* 17 metric values from the report's table */ },
        "parse_warnings": []
      },

      "ground_truth": {
        "metrics": { /* 17 ground-truth values from verify_weekly_metrics */ }
      },

      "metric_diffs": {
        "week_close": { "report": 132.50, "truth": 132.49, "abs_diff": 0.01, "rel_diff_pct": 0.008 },
        "macd_signal": { "report": "bullish_strengthening", "truth": "bullish_strengthening", "match": true }
        /* ... one entry per metric ... */
      },

      "evidence_check": {
        "news_ids_cited": [12, 47, 88],
        "leakage_check": [ /* output of check_news_leakage */ ],
        "claimed_themes_verified": [
          { "theme": "AI chip demand", "found_in_news": true, "matches": 5 }
        ]
      },

      "forward_performance": {
        "horizons": { /* output of get_forward_returns */ },
        "rating_outcome_5d": null,
        "rating_outcome_20d": null
      },

      "scores": {
        "quantitative_alignment": null,
        "structure_and_format": null,
        "metadata_accuracy": null,
        "evidence_fidelity": null,
        "reasoning_quality": null
      },

      "notes": "Free-text observations. Use this until rubric is calibrated."
    }
  ],

  "run_metrics": {
    "rating_distribution": {"BUY": 5, "HOLD": 7, "SELL": 1},
    "mean_forward_return_per_rating_5d": {"BUY": 1.2, "HOLD": -0.1, "SELL": -2.4},
    "mean_forward_return_per_rating_20d": {"BUY": 3.5, "HOLD": 0.8, "SELL": -4.1},
    "n_with_full_horizons": 13,
    "hit_rate_5d": null,
    "hit_rate_20d": null,
    "mean_dimension_scores": {
      "quantitative_alignment": null,
      "structure_and_format": null,
      "metadata_accuracy": null,
      "evidence_fidelity": null,
      "reasoning_quality": null
    }
  },

  "overall_assessment": "2-4 sentence narrative summary of the run.",
  "consistent_strengths": [ "Example strength" ],
  "consistent_weaknesses": [ "Example weakness" ]
}
```

Why so many `null`s in V1: dimension scores and `hit_rate_*` are gated on
the rubric calibration. Everything else (extraction, ground truth, diffs,
forward returns, run aggregations on raw numbers) is fully computable and
should be populated.

---

## Writing the Result

```bash
python3 .claude/skills/report_evaluation/scripts/upsert_evaluation.py \
    --ticker NVDA \
    --agent codex \
    --model gpt-5 \
    --output-root /path/to/results/report_evaluation \
    <<'JSON'
{...final evaluation json...}
JSON
```

The script owns file naming and final write-to-disk.

---

## Hard Constraints

- Do not use network data.
- Do not read parquet directly.
- Phase 2 (per-report scoring) MUST use only data dated `<= report_date`.
- Phase 3 (run backtest) MUST use forward returns only for backtest
  aggregation, never to retroactively change Phase 2 scores.
- Do not invent metric values, forward returns, or news evidence.
- Do not write intermediate scripts to disk.
- Do not produce partial output; cover every report in the run.
- Until rubric calibration is complete, leave dimension scores and rating
  outcomes as `null` and write observations to the `notes` field.

---

## Success Condition (V1)

You succeed when you have:

1. discovered the correct report files for the requested run (Phase 1)
2. for each report, extracted its claims AND recomputed ground truth using
   `<= report_date` data only (Phase 2)
3. for each report, computed forward returns using `> report_date` data
   (Phase 3)
4. populated the run-level mechanical aggregations (rating distribution,
   mean forward return per rating)
5. produced one JSON artifact written via `upsert_evaluation.py`
6. left dimension scores and hit-rate fields as `null` with rich `notes`
   pending rubric calibration

---

## Calibration Roadmap (after V1 lands)

1. Generate ~15–20 weekly reports across 2–3 tickers and 2 agents using
   the latest `report_generation` skill.
2. Run this V1 skill on the sample. Inspect the populated `metric_diffs`
   and `forward_performance` data.
3. Look for empirical patterns:
   - typical magnitude of metric mismatches → sets numeric tolerance
   - frequency of categorical mismatches → sets categorical penalty weight
   - distribution of 5d / 20d forward returns → sets HOLD threshold
   - frequency of leakage / fabrication → sets `evidence_fidelity`
     score-cap rules
4. Replace every `TODO: calibrate from samples` in this skill with a
   concrete threshold.
5. Bump `rubric_version` from `v1-scaffold` to `v1.0` when populated.