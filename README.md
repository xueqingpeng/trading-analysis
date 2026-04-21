# trading-analysis

Five financial-agent skills (trading, pair trading, auditing, report generation, report evaluation) together with the MCP server and data plumbing they depend on. The same skill definitions are consumed two ways:

- **Claude Code** reads each skill's Markdown at `<skill>/SKILL.md` (human-readable, prose procedure).
- **openclaw** reads the YAML projection at `openclaw/skills/skill.<name>.yaml` (machine-parseable spec with routing, input schema, constraints, MCP / tool declarations, and output artifact templates).

Each YAML sets `procedure_source: ../../<skill>/SKILL.md`, so the Markdown file is the canonical source of truth and the YAML is the machine projection.

## Skills

| Skill | Output | One-line purpose |
|---|---|---|
| `trading` | `results/trading/trading_{TICKER}_{agent}_{model}.json` (upsert keyed by date) | One daily BUY/SELL/HOLD decision per invocation, driven by the `trading_mcp` DuckDB server. Caller loops over dates for replay or live mode. |
| `pair_trading` | `results/pair_trading/{agent}_pair_trading_{pair}_{model}.json` (single write at end) | Daily pair-trading simulation over 2025-03-01 to 2025-05-31. Pair selected once on the first trading day using only visible signals, then traded chronologically with LONG_SHORT / SHORT_LONG / HOLD actions. |
| `auditing` | `results/auditing/{agent}_auditing_{filing}_{ticker}_{issue}_{id}_{model}.json` (single line) | Audits one XBRL numeric fact. Compares the reported value in the instance document against the value derived from the calculation linkbase, US-GAAP taxonomy, and sign conventions (Cases A/B/C/D). |
| `report_generation` | ~13 Markdown files inside `results/report_generation/{agent}_report_generation_{ticker}_{model}/` | One weekly equity research report per Monday over a 3-month window. 8 sections, 11 required metrics, graduated Strong BUY / BUY / HOLD / SELL / Strong SELL rating. |
| `report_evaluation` | `results/report_evaluation/{agent}_report_evaluation_{ticker}_{model}.json` | Scores a run of weekly reports along five dimensions (price-prediction simulation, structure, content accuracy, evidence fidelity, reasoning) and writes one aggregate JSON per agent/ticker/model. |

## Layout

```
trading-analysis/
├── <skill>/SKILL.md              # one per skill, canonical procedure
├── trading/
│   ├── mcp/trading_mcp.py        # MCP server exposing 5 tools over DuckDB
│   ├── mcp/schema.sql            # prices / news / filings tables
│   └── db/                       # runtime DuckDB (gitignored)
├── openclaw/
│   ├── openclaw.config.example.yaml
│   ├── providers/provider.claude.yaml
│   ├── routers/router.trading-suite.yaml
│   └── skills/skill.<name>.yaml
├── tests/smoke/                  # deterministic smoke tests, no API calls
└── .mcp.json                     # wires trading_mcp into Claude Code
```

## Running under Claude Code

`.mcp.json` is already configured. From the repo root, start a Claude Code session and invoke a skill by describing the task. Examples:

- `trade AAPL on 2025-03-05`
- `run pair trading on the 10 stocks`
- `audit us-gaap:AssetsCurrent for FY2021 in the 10-K filing released by rrr on 2023-12-31 (id: mr_1)`
- `generate weekly reports for NVDA`
- `evaluate the codex reports for AAPL`

The `trading` skill requires the DuckDB at `trading/db/trading_env.duckdb` to be populated (out of band or via `tests/smoke/seed_db.py` for development). The other four skills read parquet files placed at paths referenced in each SKILL.md.

## Running under openclaw

1. Install the openclaw engine (separate project) and point it at `openclaw/openclaw.config.example.yaml`.
2. Set `ANTHROPIC_API_KEY`. Default model is `claude-sonnet-4-6`; `claude-opus-4-7` is declared as the large-context alternative for report evaluation.
3. The router dispatches with priority `pair_trading (95) > report_evaluation (92) > report_generation (90) > auditing (88) > trading (85)`, so `"pair"` wins over bare `"trade"` and `"evaluate reports"` wins over bare `"report"`.

The openclaw skill schema extends the template with `mcp_servers`, `tools`, `runtime`, `input_artifacts`, `output_artifacts`, `constraints`, `procedure`, and `procedure_source`. The openclaw engine needs loaders for these fields to execute the procedural skills.

## Smoke tests

Deterministic verification of the trading MCP server and the SKILL.md decision branches. No API calls.

```bash
python tests/smoke/seed_db.py         # creates a small AAPL DuckDB
python tests/smoke/run_tools.py       # exercises all 5 MCP tools
python tests/smoke/run_skill_logic.py # walks the decision branches
```

See `tests/smoke/README.md` for details.

## Dependencies

- Python 3.11+
- `duckdb`, `fastmcp`, `pandas`, `numpy`, `pydantic`
- `pandas-ta` is declared in `trading/mcp/requirements.txt`, but every PyPI version compatible with Python 3.11 has been yanked. On 3.11 use the shim at `tests/smoke/pandas_ta_shim.py` (50 lines, covers `sma` / `rsi` / `bbands` / `macd`), or upgrade to Python 3.12, or inline the four indicators into `trading_mcp.py`.
