# Claude Agent Trading（中文文档）

通过 [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/agent-sdk) 自动运行 `financial_agentic_benchmark` 的四个 skill：**trading**、**report_generation**、**report_evaluation**、**auditing**。

Skill 定义在项目自带的 `.claude/skills/` 目录下，Agent SDK 通过 `setting_sources=["project"]` 自动发现加载，无需手动注册。

[English version → README.md](README.md)

---

## 快速开始（推荐：Docker）

Docker 镜像内置了 Python 3.12、`claude` CLI、所有 Python 依赖，以及内置的 `claude_proxy`——当 API key 不是 `sk-ant-*` 时自动把 Claude API 协议请求转到 OpenAI / Gemini / Azure。host 上除了 Docker 外无需安装其它东西。

```bash
# 1. 构建镜像（只需一次）
./build_docker.sh

# 2. 用 host 路径直接调用；脚本自动挂载并重写为容器内路径
export ANTHROPIC_API_KEY=sk-xxx                         # 任一 provider 的 key
export CLAUDE_MODEL=gpt-5.4                             # 或 claude-sonnet-4-6 等

./run_docker.sh trading --verbose \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path ./data/trading.duckdb \
    --output  ./results/trading
```

输出写到 host 的 `./results/trading/`，完整运行日志另存到 `./results/trading/run_<UTC-时间戳>.log`。

详见下方 [Docker 用法](#docker-用法)。

---

## 原生安装（非 Docker）

```bash
pip install -r requirements.txt
```

前提：系统已安装 `claude` CLI 并在 `PATH` 中可用。

复制 `.env.example` 为 `.env`，按需填写：

```bash
cp .env.example .env
```

| 环境变量 | 说明 |
|----------|------|
| `ANTHROPIC_API_KEY` | 直接 API 认证（原生 Anthropic 或任何经 proxy 的 provider key） |
| `ANTHROPIC_BASE_URL` | 自定义 API endpoint（代理、第三方兼容服务等） |
| `CLAUDE_MODEL` | 默认模型（默认 `claude-sonnet-4-6`，CLI `--model` 优先级更高） |
| `ANTHROPIC_FOUNDRY_RESOURCE` | Azure Foundry resource name |
| `ANTHROPIC_FOUNDRY_API_KEY` | Azure Foundry API key |
| `CLAUDE_CODE_USE_FOUNDRY` | 设为 `1` 启用 Foundry 模式（三个 Foundry 变量需同时设置） |

模型优先级：CLI `--model` > `CLAUDE_MODEL` 环境变量 > 默认 `claude-sonnet-4-6`。

`providers.py` 会自动把主模型 pin 到所有别名（`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL`），让 claude CLI 的内部调用（compact、title、子代理等）全部走同一个 endpoint。走 proxy 到第三方 API 时这一步是必要的，否则内置的 haiku/opus 模型名会 404。

---

## 用法

### 单任务

```bash
# trading — 按日期区间在 DuckDB 上跑单日 trading skill
python run_benchmark.py trading \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path  /path/to/trading.duckdb \
    --output   /path/to/results/trading

# report-generation — 3 个月窗口内按周生成股票研报
python run_benchmark.py report-generation \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA

# report-evaluation — 评估 report-generation 产出的 .md 报告
python run_benchmark.py report-evaluation \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA --target-agent codex --target-model gpt-5

# auditing — 对 XBRL 数值事实做审计
python run_benchmark.py auditing \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --filing-name 10k --ticker rrr --issue-time 20231231 \
    --concept-id us-gaap:AssetsCurrent \
    --period "2023-01-01 to 2023-12-31" --case-id mr_1
```

### 批量任务

准备 JSONL（每行一个任务，每条都需带 `benchmark_root`）：

```jsonl
{"task_type":"report_generation","ticker":"TSLA","benchmark_root":"/path/to/financial_agentic_benchmark"}
{"task_type":"auditing","ticker":"rrr","filing_name":"10k","issue_time":"20231231","concept_id":"us-gaap:AssetsCurrent","period":"2023-01-01 to 2023-12-31","case_id":"mr_1","benchmark_root":"/path/to/financial_agentic_benchmark"}
```

运行：

```bash
python run_benchmark.py batch \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --tasks-file tasks.jsonl
```

### 常用选项

| 选项 | 说明 |
|------|------|
| `--model` | 覆盖默认模型 |
| `--max-turns` | Agent 最大轮数（默认 30，trading 是按天） |
| `--max-budget` | 单次费用上限 USD（trading 默认 1.0/天；其它 5.0） |
| `--verbose` / `-v` | 把 assistant 文本、思考过程、工具调用事件打到 stderr |
| `--json` | 最终结果以 JSON 输出 |

---

## Docker 用法

### 文件清单

- `docker/Dockerfile` — Python 3.12-slim + Node 20 + `claude` CLI + 所有 Python 依赖 + tini
- `docker/entrypoint.sh` — 容器内启动脚本；按 API key 判断是否拉起 `claude_proxy`，再 `exec` benchmark CLI
- `.dockerignore` — 排除 `.env`、运行结果、外部 benchmark 数据
- `run_docker.sh` — host 侧启动器；自动挂载路径、透传环境变量、以 host UID/GID 运行、把完整运行日志 tee 到 `--output`
- `build_docker.sh` — 一行式 `docker build` 包装

### 构建

```bash
./build_docker.sh              # 构建 `trading-analysis:latest`
IMAGE=my-org/trading ./build_docker.sh   # 自定义 tag
```

### 运行

```bash
export ANTHROPIC_API_KEY=sk-xxx
export CLAUDE_MODEL=gpt-5.4                 # 或 claude-sonnet-4-6、gpt-4.1 等

./run_docker.sh trading --verbose \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path ./data/trading.duckdb \
    --output  ./results/trading
```

`run_docker.sh` 自动替你做：

1. **路径参数自动翻译。** 以下 CLI 参数被识别为 host 路径；脚本挂载这些路径并把参数值重写为容器内路径。你始终用 host 路径传参：

   | 分类 | 参数 | 挂载模式 |
   |------|------|---------|
   | 输入目录 | `--benchmark-root`、`--data-root`、`--reports-root` | `:ro`（挂自身） |
   | 输入文件 | `--tasks-file` | `:ro`（挂父目录） |
   | DB 文件 | `--db-path` | `:rw`（挂父目录，DuckDB 要写 WAL） |
   | 输出目录 | `--output`、`--output-root` | `:rw`（不存在则 `mkdir -p`） |

2. **环境变量透传。** host 上 export 过的以下变量会自动 `-e` 注入容器：`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`CLAUDE_MODEL`、`ANTHROPIC_FOUNDRY_RESOURCE`、`ANTHROPIC_FOUNDRY_API_KEY`、`CLAUDE_CODE_USE_FOUNDRY`、`AZURE_API_VERSION`、`PROXY_PORT`、`PROXY_VERBOSE`。

3. **Provider 路由判断（容器内 entrypoint）：**
   - `ANTHROPIC_API_KEY=sk-ant-*` → Anthropic 直连，不启 proxy
   - Foundry 三件套齐全 → 直连 Azure Foundry，不启 proxy
   - 已显式设置 `ANTHROPIC_BASE_URL` → 尊重用户，不启 proxy
   - 其它（OpenAI `sk-…`、Gemini `AIzaSy…`、Azure OpenAI `azure:…`）→ 启动 `claude_proxy` 监听 `127.0.0.1:18080`，并把 `ANTHROPIC_BASE_URL` 指向它

4. **自动记录运行日志。** 命令里带 `--output` 或 `--output-root` 时，容器的全部 stdout+stderr 会在 host 侧 tee 到 `{output 目录}/run_<UTC-时间戳>.log`。想记录思考过程 / 工具调用，需要给 benchmark 加 `--verbose`。

5. **以 host UID/GID 运行。** 容器内 `-u $(id -u):$(id -g)`，输出文件归 host 当前用户所有（不是 root）。这也是必要的——`claude` CLI 拒绝以 root 身份接受 `--dangerously-skip-permissions`。

### 调试 shell

```bash
./run_docker.sh bash
```

进入容器交互 shell，不启 proxy、不跑 benchmark。用来 `claude --version`、检查 Python import、看 `.claude/skills/` 等。

### Proxy 日志

默认情况下 `claude_proxy` 的访问日志会被重定向到容器内的 `/tmp/claude_proxy.log`，避免遮盖 benchmark 的 `--verbose` 思考输出。想直接在前台看：

```bash
PROXY_VERBOSE=1 ./run_docker.sh trading --verbose ...
```

或从运行中的容器里取：

```bash
docker exec <container-id> cat /tmp/claude_proxy.log
```

### 强制重建

```bash
./run_docker.sh --build -- --help    # 强制重建镜像再跑
./build_docker.sh                    # 独立重建
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

## 项目结构

```
trading-analysis/
├── .claude/skills/             # Agent SDK 自动发现的 skill 定义
│   ├── trading/                # SKILL.md + scripts/（MCP server、upsert_decision.py…）
│   ├── pair_trading/
│   ├── report_generation/
│   ├── report_evaluation/
│   └── auditing/
├── claude_agent_trading/       # Python 包
│   ├── core.py                 # Agent SDK 调用封装
│   ├── benchmark.py            # 任务定义与 prompt 构建
│   ├── benchmark_cli.py        # CLI 参数解析
│   ├── trading_daily.py        # 单日 trading skill 的 daily-loop orchestrator
│   └── providers.py            # API key / .env / 模型别名解析
├── claude_proxy/
│   ├── proxy.py                # Claude API → OpenAI 兼容协议代理（端口 18080）
│   └── test_proxy.py
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── tests/
├── run_benchmark.py            # CLI 入口
├── run_docker.sh               # Docker host 侧启动脚本
├── build_docker.sh             # 镜像构建脚本
├── .dockerignore
├── .env.example
└── requirements.txt
```
