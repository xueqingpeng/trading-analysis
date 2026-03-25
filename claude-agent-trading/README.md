# Claude Agent Trading

通过 [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/agent-sdk) 自动运行 `financial_agentic_benchmark` 的四个 skill：trading、report_generation、report_evaluation、auditing。

Skill 定义在项目自带的 `.claude/skills/` 目录下，Agent SDK 通过 `setting_sources=["project"]` 自动发现和加载，无需手动注册。

## 安装

```bash
pip install -r requirements.txt
```

前提：系统已安装 `claude` CLI 并在 PATH 中可用。

## 配置

复制 `.env.example` 为 `.env`，按需填写：

```bash
cp .env.example .env
```

| 环境变量 | 说明 |
|----------|------|
| `ANTHROPIC_API_KEY` | Direct API 认证 |
| `ANTHROPIC_BASE_URL` | 自定义 API endpoint（代理、第三方兼容服务等） |
| `CLAUDE_MODEL` | 默认模型（默认 `claude-sonnet-4-6`，CLI `--model` 优先级更高） |
| `ANTHROPIC_FOUNDRY_RESOURCE` | Azure Foundry resource name |
| `ANTHROPIC_FOUNDRY_API_KEY` | Azure Foundry API key |
| `CLAUDE_CODE_USE_FOUNDRY` | 设为 `1` 启用 Foundry 模式（三个 Foundry 变量需同时设置） |

优先级：CLI `--model` > `CLAUDE_MODEL` 环境变量 > 默认值 `claude-sonnet-4-6`

## 用法

所有命令都需要 `--benchmark-root` 指向 `financial_agentic_benchmark` 目录。

### 单任务

```bash
python run_benchmark.py trading \
  --benchmark-root /path/to/financial_agentic_benchmark \
  --ticker TSLA

python run_benchmark.py report-generation \
  --benchmark-root /path/to/financial_agentic_benchmark \
  --ticker TSLA

python run_benchmark.py report-evaluation \
  --benchmark-root /path/to/financial_agentic_benchmark \
  --ticker TSLA --target-agent codex --target-model gpt-5

python run_benchmark.py auditing \
  --benchmark-root /path/to/financial_agentic_benchmark \
  --filing-name 10k --ticker rrr --issue-time 20231231 \
  --concept-id us-gaap:AssetsCurrent \
  --period "2023-01-01 to 2023-12-31" --case-id mr_1
```

### 批量任务

准备 JSONL 文件（每行一个任务），每个任务必须包含 `benchmark_root`：

```jsonl
{"task_type":"trading","ticker":"TSLA","benchmark_root":"/path/to/financial_agentic_benchmark"}
{"task_type":"report_generation","ticker":"TSLA","benchmark_root":"/path/to/financial_agentic_benchmark"}
```

执行：

```bash
python run_benchmark.py batch \
  --benchmark-root /path/to/financial_agentic_benchmark \
  --tasks-file tasks.jsonl
```

### 常用选项

| 选项 | 说明 |
|------|------|
| `--model` | 覆盖默认模型（默认 `claude-sonnet-4-6`） |
| `--max-turns` | Agent 最大轮数（默认 30） |
| `--max-budget` | 单次运行费用上限 USD（默认 5.0） |
| `--verbose` / `-v` | 打印 agent 的 assistant text 和 tool use 到 stderr |
| `--json` | 以 JSON 格式输出结果 |

## Python API

```python
from claude_agent_trading import BenchmarkTask, run_benchmark_task

result = run_benchmark_task(
    BenchmarkTask(
        task_type="trading",
        ticker="TSLA",
        benchmark_root="/path/to/financial_agentic_benchmark",
    )
)

print(result.agent_result.result)
print(f"Cost: ${result.agent_result.cost_usd:.4f}")
```

## 项目结构

```
claude-agent-trading/
├── .claude/skills/          # Agent SDK 自动发现的 skill 定义
│   ├── trading/SKILL.md
│   ├── report_generation/SKILL.md
│   ├── report_evaluation/SKILL.md
│   └── auditing/SKILL.md
├── claude_agent_trading/    # Python 包
│   ├── core.py              # Agent SDK 调用封装
│   ├── benchmark.py         # 任务定义与 prompt 构建
│   ├── benchmark_cli.py     # CLI 参数解析
│   └── providers.py         # API key / .env 加载
├── tests/
├── run_benchmark.py         # 入口脚本
├── .env.example
└── requirements.txt
```
