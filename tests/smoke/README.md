# Trading skill smoke tests

Deterministic smoke tests for the `trading` skill's MCP server and
decision branches. No Claude API calls, no network, no external data
dependencies.

## What these verify

| Script | What it exercises |
|---|---|
| `seed_db.py` | Creates a minimal `trading/db/trading_env.duckdb` with ~32 days of AAPL prices (plus a deliberate MLK forward-fill row), 5 news rows, and 1 10-Q filing. |
| `run_tools.py` | Imports `trading/mcp/trading_mcp.py` and calls each of the 5 MCP tools (`get_latest_date`, `get_prices`, `get_news`, `get_filings`, `get_indicator`) against the seeded DB. Asserts expected shapes. |
| `run_skill_logic.py` | Walks through the SKILL.md decision branches (`target_date` resolution, data-missing STOP, weekend / forward-fill HOLD, normal decide+upsert, idempotent re-run). Writes a sample action-list JSON under `tests/smoke/results/trading/`. |

## Dependencies

- Python 3.11+
- `duckdb`, `fastmcp`, `pandas`, `numpy`, `pydantic`
- `pandas-ta` is **not** required. `pandas_ta_shim.py` provides a 50-line
  drop-in replacement covering `sma`, `rsi`, `bbands`, `macd` because
  PyPI's Python-3.11-compatible `pandas-ta` versions are all yanked. The
  shim is injected into `sys.modules` before importing `trading_mcp`.

## Run

From the repo root:

```bash
python tests/smoke/seed_db.py
python tests/smoke/run_tools.py
python tests/smoke/run_skill_logic.py
```

`TRADING_DB_PATH` is respected if you want to point at a different
DuckDB file. Default is `{repo_root}/trading/db/trading_env.duckdb`.

## Expected outcome

- `run_tools.py` ends with `ALL 5 TOOLS OK`.
- `run_skill_logic.py` prints 6 case blocks and a final action-list JSON
  with 2 records (MLK HOLD + 2025-02-14 BUY).
- `tests/smoke/results/trading/trading_AAPL_claude-code_claude-sonnet-4-6.json`
  is written (gitignored).

## Known limitations

- `get_indicator(..., 'macd')` returns `[]` on the 32-day seed because
  MACD needs 26 (slow) + 9 (signal) non-NaN periods. This is correct
  behavior, not a bug. Extend the seed window past ~40 trading days to
  exercise MACD.
- The seed does not forward-fill weekends, so the weekday-check branch
  in `run_skill_logic.py` hits the data-missing STOP instead. The
  forward-fill branch IS exercised via the MLK row.
