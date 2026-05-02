# Claude Agent Trading（中文文档）

通过 [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/agent-sdk) 自动运行 `financial_agentic_benchmark` 的五个 skill：**trading**、**hedging**、**report_generation**、**report_evaluation**、**auditing**。

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
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
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
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path  ./env.duckdb \
    --output   /path/to/results/trading

# report-generation — 按日期区间在 DuckDB 上跑 weekly-loop 报告生成
python run_benchmark.py report-generation \
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
    --output  /path/to/results/report_generation

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
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
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

## Task 速查 — Docker CLI vs 原始 prompt

调用 skill 有两种方式：

1. **Docker / runner CLI** — `./run_docker.sh <task> ...`。Runner 会自动构造每天 / 每个样本的 prompt 并发给 agent。**正常使用就用这种**。
2. **原始 prompt** — agent 在 SDK 调用里实际看到的字符串。如果你要自己驱动 skill（比如直接用 `claude` CLI、自写 SDK 脚本、或者套别的 orchestrator），用这种。`.claude/skills/` 下的 skill 文件不关心是谁发的 prompt。

下面 5 个 skill 两种形式都列出来。原始 prompt 是 `claude_agent_trading/` 里 `build_*_prompt` 函数的逐字输出，可以直接粘进 `claude` CLI 调用。

### trading — 每天对一只股票 BUY/SELL/HOLD

**Runner（按日期循环，每天一次 agent 调用）：**

```bash
./run_docker.sh trading --verbose \
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
    --output  ./results/trading
```

**单天发给 agent 的 raw prompt：**

```
Trade TSLA on 2026-01-02.

Your turn is NOT complete unless you have actually invoked the Bash tool to run
`python3 .claude/skills/trading/scripts/upsert_decision.py` with all required
flags. A text-only response that merely describes or announces the decision is
a FAILURE — the result file will not exist on disk. Do not stop, do not write
a summary, do not say the decision has been recorded until the Bash call has
returned its one-line JSON success summary.

When calling upsert_decision.py, pass --output-root=/io/slot1 and
--model=gpt-5.4 exactly as given (do not substitute your own model name).
```

**断点续传行为：** 每一天都会再发给 agent 跑。输出文件以 `--symbol` × `--model` 为粒度（一个 JSON），重跑同一日期会**覆盖**该日记录（`upsert_decision.py` 按日期 upsert）。**没有自动跳过** —— 想只填空缺，请收紧 `--start` / `--end` 范围。

### hedging — 每日配对交易决策

**Runner（按日循环；首日触发 `IS_FIRST_DAY=True` 选 pair，后续天数从盘上读已选好的 pair）：**

```bash
./run_docker.sh hedging --verbose \
    --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
    --output  ./results/hedging
```

**Raw prompt — 首日（IS_FIRST_DAY=True）：**

```
Start hedging on 2026-01-02 with IS_FIRST_DAY=True.

Your turn is NOT complete unless you have actually invoked the Bash tool to run
`python3 .claude/skills/hedging/scripts/upsert_hedging_decision.py` with all
required flags. A text-only response that merely describes or announces the
decision is a FAILURE — the result file will not exist on disk. Do not stop,
do not write a summary, do not say the decision has been recorded until the
Bash call has returned its one-line JSON success summary.

When calling upsert_hedging_decision.py, pass --output-root=/io/slot1 and
--model=gpt-5.4 exactly as given (do not substitute your own model name).
```

**Raw prompt — 后续天（IS_FIRST_DAY=False，默认值）：**

```
Run hedging for 2026-01-05 with IS_FIRST_DAY=False.

[末尾的 Bash + --output-root + --model 段落跟上面相同，只是脚本名是
upsert_hedging_decision.py]
```

**断点续传行为：** Runner 先检查 `--output`。如果里面已经有 `hedging_*_<model>.json`：**所有**循环日都按 `IS_FIRST_DAY=False` 跑，复用文件里已选好的 pair，首日**不会**重新触发 pair selection。每日记录按日期 upsert（已有日期会覆盖）。想强制重新选 pair，把 `--output` 指向干净空目录，或删掉现有的 `hedging_*.json`。

### auditing — XBRL 数值事实审计

**Runner — 单 case（per-case flags）：**

```bash
./run_docker.sh auditing --verbose \
    --filing-name 10k --ticker rrr --issue-time 20231231 \
    --concept-id us-gaap:AssetsCurrent \
    --period "2023-01-01 to 2023-12-31" --case-id mr_1 \
    --data-root   ./auditing_env \
    --output-root ./results/auditing
```

**Runner — 批量（一行一个 prompt 的 txt 文件，自动 resume 已存在输出）：**

```bash
./run_docker.sh auditing --verbose \
    --tasks-file  ./prompts/auditing.txt \
    --data-root   ./auditing_env \
    --output-root ./results/auditing
```

tasks 文件里用 `{env_dir}` / `{result_dir}` 占位符，runner 自动替换；resume 通过预测 `write_audit.py` 写出来的文件名 + 看磁盘是否已存在来判断跳过。

**单 case 发给 agent 的 raw prompt：**

```
Please audit the value of us-gaap:AssetsCurrent for 2023-01-01 to 2023-12-31
in the 10k filing released by rrr on 2023-12-31. What's the reported value?
What's the actual value calculated from the relevant linkbases and US-GAAP
taxonomy? (id: mr_1) The input data is at /io/slot0/auditing.

Your turn is NOT complete unless you have actually invoked the Bash tool to run
`python3 .claude/skills/auditing/scripts/write_audit.py` with all required
flags. A text-only response that merely describes the audit is a FAILURE —
the result file will not exist on disk. Do not stop, do not write a summary,
do not say the audit has been recorded until the Bash call has returned its
one-line JSON success summary.

When calling write_audit.py, pass --output-root=/io/slot1 and --model=gpt-5.4
exactly as given (do not substitute your own model name).
```

**断点续传行为：**
- **单 case 模式** —— 永远重跑。`write_audit.py` 会覆盖已存在的预测文件名。
- **批量模式** —— 默认 `resume=True`。每个 prompt 跑前 runner 先预测 `write_audit.py` 会写出的精确文件名；如果已存在 `--output-root` 里就**跳过**。跳过的 case 不消耗预算（$0），日志显示为 `SKIP (resume)`。中途 error 的 case 没产出文件 → 下次重跑会被自动重试。想强制全部重跑，加 `--no-resume`。

### report-generation — 单只股票的每周股票研究报告

**Runner（按日期循环，`[start, end]` 中每个周五调用一次 agent）：**

```bash
./run_docker.sh report-generation --verbose \
    --symbol TSLA --start 2026-01-02 --end 2026-03-31 \
    --db-path ./env.duckdb \
    --output  ./results/report_generation
```

Runner 在 `[start, end]` 内逐**周五**迭代。如果 `--start` 不是周五，自动前进到下一个周五。某周周五是市场假期时，skill 内部的 `is_trading_day` 会自动 fallback 到前一交易日（通常是周四）。每个 TARGET_DATE 对应的"报告周"是该 ISO 周的周一 → TARGET_DATE，所以周一是假日时窗口自动收缩到 4 个交易日，**不会跨上周**。

**单个周五发给 agent 的 raw prompt：**

```
Generate the weekly equity research report for TSLA for the week ending 2026-01-02.

Your turn is NOT complete unless you have actually invoked the Bash tool to run
`python3 .claude/skills/report_generation/scripts/upsert_report.py` with all
required flags AND piped the full Markdown report on stdin. A text-only response
that merely describes or announces the report is a FAILURE — the result file
will not exist on disk. Do not stop, do not write a summary, do not say the
report has been written until the Bash call has returned its one-line JSON
success summary.

When calling upsert_report.py, pass --symbol=TSLA --target-date=2026-01-02
--output-root=/io/slot1 and --model=gpt-5.4 exactly as given (do not substitute
your own model name).
```

**输出（`upsert_report.py` 写入）：**

- `<output>/report_generation_<symbol>_<model>.json` — 总览记录列表，每生成一周追加一条
- `<output>/report_generation_<symbol>_<model>/report_generation_<symbol>_<YYYYMMDD>_<model>.md` — 每周一份 Markdown 正文

**断点续传行为：** 每个迭代到的周五都会再发给 agent 跑。输出文件以 `--symbol` × `--model` 为粒度（一个 summary JSON），重跑同一周会**覆盖**该周记录（`upsert_report.py` 按 target-date upsert），同时改写该周的 `.md`。**没有自动跳过** —— 想只填空缺，请收紧 `--start` / `--end` 范围。

### report-evaluation — 给之前 report-generation 跑出来的报告打分

**Runner：**

```bash
./run_docker.sh report-evaluation --verbose \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA \
    --target-agent codex --target-model gpt-5
```

**Raw prompt：**

```
Evaluate the codex/TSLA/gpt-5 run. Reports parent:
/io/slot0/results/report_generation. Data: /io/slot0/data/trading.
Output: /io/slot1/results/report_evaluation.
```

### 直接通过 `claude` CLI 驱动 skill

上面那些 raw prompt 已经包含 Claude Code agent 需要的全部信息——SDK / CLI 会从 cwd 自动发现 `.claude/skills/<name>/SKILL.md` + 匹配的 `.mcp.json`。所以你完全可以绕开 runner：

```bash
cd /path/to/trading-analysis        # 让 .claude/skills/ 可被发现
claude --print --model gpt-5.4 \
    --mcp-config .claude/skills/auditing/.mcp.json \
    "Please audit the value of us-gaap:AssetsCurrent for 2023-01-01 to 2023-12-31
in the 10k filing released by rrr on 2023-12-31..."
```

Runner 在 raw prompt 之上多做了三件事：(1) 按日 / 按 case 循环；(2) 给 MCP 注入 `--db-path` / `--data-root`；(3) 友好处理 PermissionError + `--output-root` / `--model` 钉死。如果你绕开 runner，这三件事得自己负责。

---

## Python API

每个 runner 都对应一个 range-runner 函数，可以直接在 Python 里调用：

```python
from datetime import date
from pathlib import Path
from claude_agent_trading import (
    ReportGenerationWeeklyConfig,
    run_report_generation_range,
)

result = run_report_generation_range(
    ReportGenerationWeeklyConfig(
        symbol="TSLA",
        start=date(2026, 1, 2),
        end=date(2026, 3, 31),
        output_dir=Path("./results/report_generation").resolve(),
        db_path=Path("./env.duckdb").resolve(),
    )
)

print(f"已生成周数: {len(result.per_week)}")
print(f"错误数: {result.num_errors}")
print(f"总费用: ${result.total_cost_usd:.4f}")
```

trading / hedging / auditing 的对应入口分别是 `run_trading_range(TradingDailyConfig)`、`run_hedging_range(HedgingDailyConfig)`、`run_auditing(AuditingConfig)` / `run_auditing_batch(AuditingBatchConfig)`。旧的 `BenchmarkTask` + `run_benchmark_task` 路径现在只服务于 `report-evaluation`。

---

## 项目结构

```
trading-analysis/
├── .claude/skills/             # Agent SDK 自动发现的 skill 定义
│   ├── trading/                # SKILL.md + scripts/（MCP server、upsert_decision.py…）
│   ├── hedging/
│   ├── report_generation/
│   ├── report_evaluation/
│   └── auditing/
├── claude_agent_trading/       # Python 包
│   ├── core.py                 # Agent SDK 调用封装
│   ├── benchmark.py            # 任务定义与 prompt 构建
│   ├── benchmark_cli.py        # CLI 参数解析
│   ├── trading_daily.py        # 单日 trading skill 的 daily-loop orchestrator
│   ├── hedging_daily.py        # 单日 hedging skill 的 daily-loop orchestrator
│   ├── report_generation_weekly.py  # report_generation 的 weekly-loop orchestrator（[start, end] 内每个周五一次）
│   ├── auditing_runner.py      # auditing 的单 case + 批量 orchestrator
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
