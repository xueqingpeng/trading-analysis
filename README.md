# Claude Agent Trading

Run the four `financial_agentic_benchmark` skills — **trading**, **report_generation**, **report_evaluation**, **auditing** — via the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/agent-sdk).

Skills live under `.claude/skills/` and are auto-discovered by the SDK through `setting_sources=["project"]`; no manual registration is needed.

[中文文档 / Chinese version → README_ZH.md](README_ZH.md)

---

## Quick Start (Docker — recommended)

The Docker image bundles Python 3.12, the `claude` CLI, all Python dependencies, and a built-in `claude_proxy` that transparently routes Claude API calls to OpenAI / Gemini / Azure when your key is not `sk-ant-*`. You don't need to install anything on the host besides Docker.

```bash
# 1. Build the image once
./build_docker.sh

# 2. Run a task — pass host paths directly; the script auto-mounts them
export ANTHROPIC_API_KEY=sk-xxx                         # any provider key
export CLAUDE_MODEL=gpt-5.4                             # or claude-sonnet-4-6, etc.

./run_docker.sh trading --verbose \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path ./data/trading.duckdb \
    --output  ./results/trading
```

Output lands in `./results/trading/` on the host. A full run log is also written to `./results/trading/run_<UTC-timestamp>.log`.

See [Docker usage](#docker-usage) below for details.

---

## Native Install

```bash
pip install -r requirements.txt
```

Prerequisite: the `claude` CLI must be installed and on `PATH`.

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

| Environment variable | Description |
|----------------------|-------------|
| `ANTHROPIC_API_KEY` | Direct API authentication (native Anthropic, or any proxied provider key) |
| `ANTHROPIC_BASE_URL` | Custom API endpoint (proxy, OpenAI-compatible service, etc.) |
| `CLAUDE_MODEL` | Default model (defaults to `claude-sonnet-4-6`; CLI `--model` takes precedence) |
| `ANTHROPIC_FOUNDRY_RESOURCE` | Azure Foundry resource name |
| `ANTHROPIC_FOUNDRY_API_KEY` | Azure Foundry API key |
| `CLAUDE_CODE_USE_FOUNDRY` | Set to `1` to enable Foundry mode (all three Foundry vars required) |

Model resolution order: CLI `--model` > `CLAUDE_MODEL` env > default `claude-sonnet-4-6`.

`providers.py` automatically pins the main model to every alias (`ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`), so internal Claude CLI calls (compaction, title generation, sub-agents) all hit the same endpoint — critical when routing through a proxy to third-party APIs that don't recognize the built-in haiku/opus model names.

---

## Usage

### Single task

```bash
# trading — daily-loop skill over a date range, queries DuckDB via MCP
python run_benchmark.py trading \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path  /path/to/trading.duckdb \
    --output   /path/to/results/trading

# report-generation — weekly equity reports over a 3-month window
python run_benchmark.py report-generation \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA

# report-evaluation — scores reports produced by report-generation
python run_benchmark.py report-evaluation \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA --target-agent codex --target-model gpt-5

# auditing — XBRL numeric fact audit
python run_benchmark.py auditing \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --filing-name 10k --ticker rrr --issue-time 20231231 \
    --concept-id us-gaap:AssetsCurrent \
    --period "2023-01-01 to 2023-12-31" --case-id mr_1
```

### Batch

Prepare a JSONL file (one task per line, each including `benchmark_root`):

```jsonl
{"task_type":"report_generation","ticker":"TSLA","benchmark_root":"/path/to/financial_agentic_benchmark"}
{"task_type":"auditing","ticker":"rrr","filing_name":"10k","issue_time":"20231231","concept_id":"us-gaap:AssetsCurrent","period":"2023-01-01 to 2023-12-31","case_id":"mr_1","benchmark_root":"/path/to/financial_agentic_benchmark"}
```

Run:

```bash
python run_benchmark.py batch \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --tasks-file tasks.jsonl
```

### Common options

| Option | Description |
|--------|-------------|
| `--model` | Override the default model |
| `--max-turns` | Agent turn cap (default 30, `trading` is per-day) |
| `--max-budget` | Cost cap in USD (`trading` default 1.0/day; others 5.0) |
| `--verbose` / `-v` | Print assistant text, thinking, and tool-use events to stderr |
| `--json` | Emit the final result as JSON |

---

## Docker usage

### Files

- `docker/Dockerfile` — Python 3.12-slim + Node 20 + `claude` CLI + all Python deps + tini
- `docker/entrypoint.sh` — runs inside the container; decides whether to start `claude_proxy` based on the API key, then `exec`s the benchmark CLI
- `.dockerignore` — excludes `.env`, results, and external benchmark data from the build context
- `run_docker.sh` — host-side launcher; auto-mounts path arguments, forwards env vars, runs as your UID/GID, and tees the full run log into `--output`
- `build_docker.sh` — one-line `docker build` wrapper

### Building

```bash
./build_docker.sh              # builds `trading-analysis:latest`
IMAGE=my-org/trading ./build_docker.sh   # custom tag
```

### Running

```bash
export ANTHROPIC_API_KEY=sk-xxx
export CLAUDE_MODEL=gpt-5.4                 # or claude-sonnet-4-6, gpt-4.1, etc.

./run_docker.sh trading --verbose \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path ./data/trading.duckdb \
    --output  ./results/trading
```

What `run_docker.sh` does for you:

1. **Auto path translation.** The following CLI arguments are recognized as host paths; `run_docker.sh` mounts them into the container and rewrites the argument to the in-container path, so you always pass host paths:

   | Category | Arguments | Mount mode |
   |----------|-----------|-----------|
   | Input directory | `--benchmark-root`, `--data-root`, `--reports-root` | `:ro` (mounted as-is) |
   | Input file | `--tasks-file` | `:ro` (parent directory mounted) |
   | DuckDB file | `--db-path` | `:rw` (parent directory; DuckDB needs to write the WAL) |
   | Output directory | `--output`, `--output-root` | `:rw` (created with `mkdir -p` if missing) |

2. **Environment forwarding.** These env vars, if exported on the host, are passed to the container automatically: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_MODEL`, `ANTHROPIC_FOUNDRY_RESOURCE`, `ANTHROPIC_FOUNDRY_API_KEY`, `CLAUDE_CODE_USE_FOUNDRY`, `AZURE_API_VERSION`, `PROXY_PORT`, `PROXY_VERBOSE`.

3. **Provider routing.** The in-container entrypoint inspects `ANTHROPIC_API_KEY`:
   - `sk-ant-*` → Anthropic direct, no proxy
   - Foundry vars all set → Azure Foundry, no proxy
   - `ANTHROPIC_BASE_URL` already set → respected, no proxy
   - Otherwise (OpenAI `sk-…`, Gemini `AIzaSy…`, Azure `azure:…`) → starts `claude_proxy` on `127.0.0.1:18080` and points `ANTHROPIC_BASE_URL` at it

4. **Run log capture.** If the command has an `--output` or `--output-root`, the container's full stdout+stderr are teed to `{output}/run_<UTC-timestamp>.log` on the host. Pass `--verbose` to the benchmark to capture thinking / tool-use events.

5. **Host UID/GID.** The container runs as your user (`-u $(id -u):$(id -g)`). Files in the output directory are owned by you, not root. Required because the `claude` CLI refuses `--dangerously-skip-permissions` under root.

### Debug shell

```bash
./run_docker.sh bash
```

Drops you into an interactive shell inside the image, skipping proxy startup and the benchmark. Useful for checking `claude --version`, running imports, or poking at `.claude/skills/`.

### Proxy logging

By default `claude_proxy` access logs are redirected to `/tmp/claude_proxy.log` inside the container so they don't bury benchmark `--verbose` output. To tail them on stdout:

```bash
PROXY_VERBOSE=1 ./run_docker.sh trading --verbose ...
```

Or grab the file from a still-running container:

```bash
docker exec <container-id> cat /tmp/claude_proxy.log
```

### Rebuilding

```bash
./run_docker.sh --build -- --help    # force rebuild, then run a command
./build_docker.sh                    # same, standalone
```

---

## Python API

```python
from claude_agent_trading import BenchmarkTask, run_benchmark_task

result = run_benchmark_task(
    BenchmarkTask(
        task_type="report_generation",
        ticker="TSLA",
        benchmark_root="/path/to/financial_agentic_benchmark",
    )
)

print(result.agent_result.result)
print(f"Cost: ${result.agent_result.cost_usd:.4f}")
```

---

## Project structure

```
trading-analysis/
├── .claude/skills/             # Agent SDK auto-discovers these
│   ├── trading/                # SKILL.md + scripts/ (MCP server, upsert_decision.py, ...)
│   ├── pair_trading/
│   ├── report_generation/
│   ├── report_evaluation/
│   └── auditing/
├── claude_agent_trading/       # Python package
│   ├── core.py                 # Agent SDK wrapper
│   ├── benchmark.py            # Task definitions & prompt building
│   ├── benchmark_cli.py        # CLI argument parsing
│   ├── trading_daily.py        # Daily-loop orchestrator for the trading skill
│   └── providers.py            # API key / .env / model-alias resolution
├── claude_proxy/
│   ├── proxy.py                # Claude API → OpenAI-compatible proxy (port 18080)
│   └── test_proxy.py
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── tests/
├── run_benchmark.py            # CLI entry point
├── run_docker.sh               # Docker launcher (host side)
├── build_docker.sh             # Image builder
├── .dockerignore
├── .env.example
└── requirements.txt
```
