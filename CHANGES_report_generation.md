# report_generation 重构变更文档

## 背景与动机

### 原来的设计

旧版 `report_generation` skill 是一个**批量任务**：一次调用生成整个 3 个月窗口（2025-03-01 到 2025-05-31）内所有周一的报告，直接读取 parquet 文件，不走 MCP。

问题：
- 与 `trading` skill 的架构完全不同，无法复用同一套调度框架
- 直接读 parquet 文件，绕过了 MCP 层，无法统一数据访问
- 一次生成所有报告，无法按天重跑、断点续跑
- 变量命名不统一（`REPORT_DATE` vs `TARGET_DATE`）

### 重构目标

对齐 `trading` skill 的架构：

| 维度 | 旧版 | 新版 |
|------|------|------|
| 调用粒度 | 一次生成全部报告 | 每次只生成一天的报告 |
| 报告频率 | 每周一（weekly） | 每个交易日（daily） |
| 数据访问 | 直接读 parquet | 通过 MCP 工具查询 DuckDB |
| MCP 服务器 | 无（或借用 trading 的） | 自己独立的 `report_generation_mcp` |
| 变量命名 | `REPORT_DATE` | `TARGET_DATE`（与 trading 统一） |
| 报告类型 | equity research report | equity research report |
| 持久化脚本 | 无标准化脚本 | `upsert_report.py`（对应 trading 的 `upsert_decision.py`） |

---

## 架构概览

```
用户调用 run_benchmark.py report-generation --symbol TSLA --start ... --end ...
    │
    ▼
report_generation_daily.py (daily-loop orchestrator)
    │  每天循环一次
    ▼
Claude Agent SDK (run_agent)
    │  加载 SKILL.md 作为 system prompt
    │  启动 report_generation_mcp MCP 服务器
    ▼
Agent 执行 SKILL.md 中的步骤：
    1. is_trading_day(SYMBOL, TARGET_DATE)  ← MCP 工具
    2. get_prices(...)                      ← MCP 工具
    3. list_news(...) / get_news_by_id(...) ← MCP 工具
    4. (可选) list_filings / get_filing_section / get_indicator
    5. 生成报告 Markdown
    6. python3 upsert_report.py --symbol ... --rating ... <<'REPORT'
       ...报告内容...
       REPORT
    │
    ▼
输出文件：
    results/report_generation/report_generation_{SYMBOL}_{model}/{SYMBOL}_{YYYYMMDD}_{model}.md
    results/report_generation/report_generation_{SYMBOL}_{model}.json  (汇总 JSON)
```

数据流：所有 trading 和 report_generation 共用同一个 `trading.duckdb` 文件，各自有独立的 MCP 服务器读取它。

---

## 文件变更清单

### 新建文件（6 个）

#### 1. `.claude/skills/report_generation/scripts/mcp/report_generation_mcp.py`

**作用**：report_generation 专属的 MCP 服务器，结构完全镜像 `trading_mcp.py`。

**为什么需要独立的 MCP 服务器**：
- 每个 skill 应该有自己的 MCP 服务器，职责清晰，互不干扰
- 命名空间独立：服务器名 `report_generation_mcp`，工具调用不会与 `trading_mcp` 混淆
- `is_trading_day` 返回字段略有不同：返回 `should_write`（而非 `should_upsert`），语义更贴合报告生成场景

**8 个 MCP 工具**（与 trading 完全对应）：

| 工具 | 用途 |
|------|------|
| `get_prices(symbol, date_start, date_end)` | 查询 OHLCV 价格数据 |
| `is_trading_day(symbol, target_date)` | 判断是否交易日，返回 `should_write` |
| `list_news(symbol, date_start, date_end)` | 列出新闻摘要（无 highlights） |
| `get_news_by_id(symbol, id)` | 获取单条新闻完整内容 |
| `list_filings(symbol, date_start, date_end)` | 列出财报摘要 |
| `get_filing_section(symbol, date, document_type, section)` | 读取财报章节内容 |
| `get_indicator(symbol, date_start, date_end, indicator)` | 计算技术指标 |

`is_trading_day` 返回结构：
```python
{
    "is_trading_day": bool,
    "reason": "trading_day" | "weekend" | "holiday" | "not_loaded",
    "prev_trading_day": "YYYY-MM-DD",
    "prev_trading_day_adj_close": float,
    "latest_date_in_db": "YYYY-MM-DD",
    "should_write": bool   # ← 注意：trading 里叫 should_upsert
}
```

---

#### 2. `.claude/skills/report_generation/scripts/mcp/schema.sql`

**作用**：DuckDB 表结构文档，与 trading 共用同一个 schema（`prices`、`news`、`filings` 三张表）。

---

#### 3. `.claude/skills/report_generation/scripts/mcp/__init__.py`

**作用**：空文件，使 `mcp/` 目录成为 Python 包。

---

#### 4. `.claude/skills/report_generation/scripts/upsert_report.py`

**作用**：持久化脚本，对应 trading 的 `upsert_decision.py`。Agent 生成报告 Markdown 后，通过 stdin 管道传给这个脚本，脚本负责写文件。

**为什么用 stdin 而不是命令行参数**：报告内容是多行 Markdown，不适合作为命令行参数传递，stdin 管道是标准做法。

**调用方式**（Agent 在 Bash 工具中执行）：
```bash
python .claude/skills/report_generation/scripts/upsert_report.py \
    --symbol TSLA \
    --target-date 2025-03-03 \
    --rating BUY \
    --model claude-sonnet-4-6 \
    --output-root /path/to/results/report_generation \
    <<'REPORT'
# Daily Investment Advice Report: TSLA
...报告内容...
REPORT
```

**参数说明**：

| 参数 | 说明 |
|------|------|
| `--symbol` | 股票代码 |
| `--target-date` | 报告日期 YYYY-MM-DD |
| `--rating` | 评级：`STRONG_BUY`、`BUY`、`HOLD`、`SELL`、`STRONG_SELL` |
| `--model` | 模型标识符（用于文件名） |
| `--output-root` | 输出根目录（可选，默认 `results/report_generation`） |

**输出文件**：
- Markdown 报告：`{output_root}/report_generation_{SYMBOL}_{model}/{SYMBOL}_{YYYYMMDD}_{model}.md`
- 汇总 JSON：`{output_root}/report_generation_{SYMBOL}_{model}.json`

成功后打印一行 JSON：
```json
{"path": "...", "rating_recorded": "BUY", "date_recorded": "2025-03-03", "total_records": 1}
```

---

#### 5. `.claude/skills/report_generation/scripts/date_offset.py`

**作用**：日期偏移计算工具，与 trading 的同名脚本完全一致。

**为什么需要**：Agent 需要计算 `TARGET_DATE - 7天`、`TARGET_DATE - 30天` 等偏移日期，不应该在 Bash heredoc 里写内联 Python，统一用这个脚本。

**用法**：
```bash
python3 .claude/skills/report_generation/scripts/date_offset.py 2025-03-10 7 30 60
# 输出：
# 7    2025-03-03
# 30   2025-02-08
# 60   2025-01-09
```

---

#### 6. `claude_agent_trading/report_generation_daily.py`

**作用**：日期范围循环调度器，镜像 `trading_daily.py`。

**核心逻辑**：
```python
for target_date in date_range(config.start, config.end):
    if config.skip_weekends and target_date.weekday() >= 5:
        continue
    prompt = build_daily_prompt(model, symbol, target_date, output_dir)
    agent_result = run_agent(prompt, mcp_servers=..., max_turns=..., ...)
    output_path = _find_output_file(output_dir, symbol, model)
    yield DailyReportResult(date=target_date, agent_result=agent_result, output_path=output_path)
```

**关键类**：

```python
@dataclass
class ReportGenerationDailyConfig:
    symbol: str
    start: date
    end: date
    output_dir: Path
    db_path: Path
    project_root: Path
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 1.0
    skip_weekends: bool = True
    fail_fast: bool = False

@dataclass
class DailyReportResult:
    date: str
    agent_result: AgentResult
    output_path: Path | None

@dataclass
class ReportGenerationRangeResult:
    config: ReportGenerationDailyConfig
    per_day: list[DailyReportResult]
    total_cost_usd: float
    num_errors: int
```

---

### 修改文件（8 个）

#### 7. `.claude/skills/report_generation/SKILL.md`

**改动前**：
- 批量生成 2025-03-01 到 2025-05-31 所有周一的报告
- 直接用 pandas 读 parquet 文件
- 变量名 `REPORT_DATE`
- 报告类型：equity research report（周报）
- 11 个周度指标（week_open, week_close, weekly_return_pct, ma_4week 等）
- 输出：每个周一一个 `.md` 文件

**改动后**：
- 每次调用只生成一天的报告
- 通过 `report_generation_mcp` MCP 工具查询 DuckDB
- 变量名 `TARGET_DATE`（与 trading 统一）
- 报告类型：投资建议报告（investment advice report）
- 11 个日度指标：

| 指标 | 定义 |
|------|------|
| `day_open` | 当日开盘价 |
| `day_close` | 当日收盘价（adj_close） |
| `day_high` | 当日最高价 |
| `day_low` | 当日最低价 |
| `daily_return_pct` | `(close - open) / open × 100` |
| `ma_5day` | 过去 5 个交易日均价 |
| `ma_20day` | 过去 20 个交易日均价 |
| `price_vs_ma20` | `above` / `below` |
| `return_20day_pct` | 过去 20 个交易日累计收益率 |
| `daily_volatility` | `(high - low) / open × 100` |
| `momentum` | 当日动量标签 |

- 报告标题：`# Daily Investment Advice Report: {TICKER}`
- 评级：Strong BUY / BUY / HOLD / SELL / Strong SELL（5 级）
- 输出通过 `upsert_report.py` 写入（stdin 管道）

**典型调用流程**（SKILL.md 中定义）：
1. `is_trading_day(SYMBOL, TARGET_DATE)` — 判断是否交易日
2. `get_prices(SYMBOL, TARGET_DATE-30d, TARGET_DATE)` — 获取近期价格
3. `list_news(SYMBOL, TARGET_DATE-7d, TARGET_DATE)` — 扫描新闻标题
4. `get_news_by_id(SYMBOL, id)` — 获取相关新闻详情
5. （可选）`list_filings` / `get_filing_section` / `get_indicator`
6. 生成报告，通过 `upsert_report.py` 写入

---

#### 8. `.claude/skills/report_generation/.mcp.json`

**改动前**：
```json
{
  "mcpServers": {
    "trading_mcp": {
      "command": "python3",
      "args": [".claude/skills/trading/scripts/mcp/trading_mcp.py"]
    }
  }
}
```

**改动后**：
```json
{
  "mcpServers": {
    "report_generation_mcp": {
      "command": "python3",
      "args": [".claude/skills/report_generation/scripts/mcp/report_generation_mcp.py"]
    }
  }
}
```

---

#### 9. `claude_agent_trading/benchmark_cli.py`

**改动前**：`report-generation` 子命令使用 `--benchmark-root`、`--ticker`、`--target-agent`、`--target-model` 等参数，走 `run_benchmark_task` 单次调用。

**改动后**：`report-generation` 子命令使用与 `trading` 完全对称的参数，走 `run_report_generation_range` 日期范围循环：

```bash
# 旧版（已废弃）
python run_benchmark.py report-generation \
    --benchmark-root /path/to/benchmark \
    --ticker TSLA --target-agent codex --target-model gpt-5

# 新版
python run_benchmark.py report-generation \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path /path/to/trading.duckdb \
    --output /path/to/results/report_generation
```

新增的辅助函数：
- `_add_report_generation_daily_args(parser)` — 注册参数
- `_run_report_generation_from_args(args, callbacks)` — 构建 config 并调用
- `_emit_report_generation_range_result(result, as_json)` — 格式化输出

---

#### 10. `claude_agent_trading/__init__.py`

新增导出：
```python
from .report_generation_daily import (
    DailyReportResult,
    ReportGenerationDailyConfig,
    ReportGenerationRangeResult,
    run_report_generation_range,
)
```

---

#### 11. `claude_agent_trading/benchmark.py`

`_build_prompt()` 中 `report_generation` 任务的 prompt 更新：

**改动前**：
```python
f"Please generate weekly equity reports for {ticker}. The input data is at {data_root}..."
```

**改动后**：
```python
f"you are {model}. Generate investment advice report for {ticker}. "
f"When calling upsert_report.py, pass --output-root={output_root}."
```

---

#### 12. `README.md`

更新 `report-generation` 使用示例：

```bash
# 旧版
python run_benchmark.py report-generation \
    --benchmark-root /path/to/financial_agentic_benchmark \
    --ticker TSLA --target-agent codex --target-model gpt-5

# 新版
python run_benchmark.py report-generation \
    --symbol TSLA --start 2025-03-03 --end 2025-03-31 \
    --db-path /path/to/trading.duckdb \
    --output /path/to/results/report_generation
```

更新 Python API 示例，使用 `ReportGenerationDailyConfig`。

---

#### 13. `README_ZH.md`

同 README.md，中文版同步更新。

---

#### 14. `run_docker.sh`

更新注释中的 `report-generation` 示例命令，从旧参数改为新参数。

---

## 数据说明

两个 skill（trading 和 report_generation）共用同一个 DuckDB 文件，数据来源是 HuggingFace 的 `TheFinAI/ab` 数据集。



---

## 验证方法

### 1. 单日测试

```bash
python run_benchmark.py report-generation \
    --symbol TSLA --start 2025-03-03 --end 2025-03-03 \
    --db-path ./data/trading.duckdb \
    --output ./results/report_generation \
    --verbose
```

预期：
- stderr 打印 `[day] 2025-03-03 → invoking agent`
- 生成 `results/report_generation/report_generation_TSLA_claude-sonnet-4-6/TSLA_20250303_claude-sonnet-4-6.md`
- 生成 `results/report_generation/report_generation_TSLA_claude-sonnet-4-6.json`

### 2. 检查输出文件

```bash
# 查看报告内容
cat results/report_generation/report_generation_TSLA_claude-sonnet-4-6/TSLA_20250303_claude-sonnet-4-6.md

# 查看汇总 JSON
cat results/report_generation/report_generation_TSLA_claude-sonnet-4-6.json
```

### 3. 多日测试

```bash
python run_benchmark.py report-generation \
    --symbol TSLA --start 2025-03-03 --end 2025-03-07 \
    --db-path ./data/trading.duckdb \
    --output ./results/report_generation
```

预期：生成 5 个 `.md` 文件（周一到周五），汇总 JSON 包含 5 条记录。

### 4. Docker 方式

```bash
export ANTHROPIC_API_KEY=sk-xxx
./run_docker.sh report-generation \
    --symbol TSLA --start 2025-03-03 --end 2025-03-07 \
    --db-path ./data/trading.duckdb \
    --output ./results/report_generation \
    --verbose
```

---

### 新增 `get_trading_decisions` MCP 工具

**为什么加**：trading 和 report_generation 是配套的——trading 每天输出 BUY/SELL/HOLD 决策，report_generation 应该能读取这些决策并在报告中引用，形成完整闭环。

**改动了 4 个文件：**

#### 1. `.claude/skills/report_generation/scripts/mcp/report_generation_mcp.py`

新增全局变量和工具：
```python
TRADING_RESULTS_ROOT: Optional[str] = None  # 新增全局变量

@mcp.tool(...)
def get_trading_decisions(symbol, date_start, date_end) -> list[dict]:
    # 扫描 {TRADING_RESULTS_ROOT}/trading_{SYMBOL}_*.json
    # 返回日期范围内的 {date, price, recommended_action, model} 记录
    # 目录不存在或无文件时返回空列表（不报错）
```

新增 CLI 参数：
```bash
--trading-results-root   # trading 结果目录，可选
```

#### 2. `claude_agent_trading/report_generation_daily.py`

`ReportGenerationDailyConfig` 新增字段：
```python
trading_results_root: Path | None = None
```

`_load_mcp_servers()` 更新，将 `--trading-results-root` 传给 MCP 服务器：
```python
def _load_mcp_servers(project_root, db_path, trading_results_root=None):
    # 如果 trading_results_root 不为 None，追加 --trading-results-root=<path>
```

`build_daily_prompt()` 文案修正（investment advice → equity research）。

#### 3. `claude_agent_trading/benchmark_cli.py`

`_add_report_generation_daily_args()` 新增可选参数：
```python
--trading-results-root   # 可选，不传也能正常跑
```

`_run_report_generation_from_args()` 将该参数传入 config。

#### 4. `.claude/skills/report_generation/SKILL.md`

工具表新增一行：
```
| get_trading_decisions(symbol, date_start, date_end) | 读取 trading skill 的 BUY/SELL/HOLD 决策 |
```

调用流程新增第 5 步（原第 5 步顺延）：
```
5. get_trading_decisions(SYMBOL, TARGET_DATE-7d, TARGET_DATE)
   — 读取近期交易信号，非空则纳入评级依据，空则跳过
```

---

### 使用方式（完整命令）

先跑 trading，再跑 report-generation 并传入 trading 结果目录：

```powershell
# 第一步：trading
python run_benchmark.py trading --symbol TSLA --start 2025-03-03 --end 2025-03-07 --db-path data/trading_env.duckdb --output results/trading

# 第二步：report-generation（引用 trading 结果）
python run_benchmark.py report-generation --symbol TSLA --start 2025-03-03 --end 2025-03-07 --db-path data/trading_env.duckdb --output results/report_generation --trading-results-root results/trading
```

`--trading-results-root` 是可选的，不传也能正常生成报告，只是报告里不会包含交易模型信号。

---

## 与 trading skill 的对比

| 维度 | trading | report_generation |
|------|---------|-------------------|
| MCP 服务器 | `trading_mcp` | `report_generation_mcp` |
| 持久化脚本 | `upsert_decision.py` | `upsert_report.py` |
| 日期工具 | `date_offset.py` | `date_offset.py`（副本） |
| 调度器 | `trading_daily.py` | `report_generation_daily.py` |
| 输出格式 | JSON（action + price） | Markdown 报告 + JSON 汇总 |
| 评级字段 | `recommended_action`（BUY/SELL/HOLD） | `rating`（5 级评级） |
| `is_trading_day` 返回 | `should_upsert` | `should_write` |
| CLI 子命令 | `trading` | `report-generation` |

---

## 补充

这次联调后又补了几处和 `report_generation` 相关的兼容性问题，避免测试通过但实际串联 `trading -> report_generation -> report_evaluation` 时出错。

### 1. 跨平台 prompt 路径格式修复

**问题**：Windows 下 `Path` 直接格式化进 prompt 时会变成反斜杠路径，例如 `\tmp\out`，而测试和 Claude CLI 提示词更稳定的形式是 POSIX 风格 `/tmp/out`。

**修复**：
- `claude_agent_trading/trading_daily.py`
- `claude_agent_trading/report_generation_daily.py`

在 `build_daily_prompt()` 中统一改为：

```python
output_dir.as_posix()
```

这样无论在 Windows 还是 Linux，传给 `upsert_decision.py` / `upsert_report.py` 的 `--output-root` 都是稳定格式。

### 2. report_generation 输出命名与 report_evaluation 协议对齐

**问题**：原先 `upsert_report.py` 写出的文件名是：

```text
report_generation_{SYMBOL}_{model}.json
report_generation_{SYMBOL}_{YYYYMMDD}_{model}.md
```

但 `report_evaluation` 的 SKILL 约定读取的是带 agent 前缀的命名：

```text
{agent}_report_generation_{SYMBOL}_{model}.json
{agent}_report_generation_{SYMBOL}_{YYYYMMDD}_{model}.md
```

如果没有 agent 前缀，评估阶段就会扫不到报告文件。

**修复**：`.claude/skills/report_generation/scripts/upsert_report.py`

新增可选参数：

```bash
--agent claude-code
```

默认值设为 `claude-code`，并将输出改为：

```text
{output_root}/{agent}_report_generation_{SYMBOL}_{model}.json
{output_root}/{agent}_report_generation_{SYMBOL}_{model}/{agent}_report_generation_{SYMBOL}_{YYYYMMDD}_{model}.md
```

同时汇总 JSON 里额外记录：

```json
{
  "agent": "claude-code"
}
```

### 3. report_generation 汇总结果 JSON 序列化修复

**问题**：`ReportGenerationRangeResult.to_dict()` 内部对 `config` 使用 `asdict()` 后，`trading_results_root` 仍然是 `Path` 对象；如果 CLI 带 `--json` 输出结果，会在 `json.dumps(...)` 时抛出：

```text
TypeError: Object of type WindowsPath is not JSON serializable
```

**修复**：`claude_agent_trading/report_generation_daily.py`

在 `to_dict()` 中显式把 `trading_results_root` 转成字符串或 `None`。

### 4. report_generation 输出文件探测逻辑同步修复

**问题**：既然输出文件名增加了 agent 前缀，`_find_output_file()` 再按旧模式：

```python
report_generation_{symbol}_*.json
```

就无法找到最新生成的汇总 JSON。

**修复**：`claude_agent_trading/report_generation_daily.py`

匹配模式改为：

```python
*_report_generation_{symbol}_*.json
```

使运行完成后的 stderr 汇总、返回结果中的 `output_path` 都能正确定位到文件。

### 5. 新增回归测试

新增测试文件：`tests/test_report_generation_daily.py`

覆盖三类问题：
- `build_daily_prompt()` 输出路径格式
- `ReportGenerationRangeResult.to_dict()` 的 JSON 序列化
- `upsert_report.py` 默认命名是否满足 `report_evaluation` 协议

同时更新了 `tests/test_trading_daily.py` 中与路径格式相关的断言。

### 6. 第三方 provider 认证排查结论

联调时还发现一个容易混淆的问题：**终端里直接运行 `claude` 能成功，不代表这个项目当前 `.env` 的路由方式也正确。**

本仓库里，手动测试成功的命令实际走的是第三方服务提供的 **Anthropic/Claude 兼容端点**，形式类似：

```bash
ANTHROPIC_API_KEY=sk-... \
ANTHROPIC_BASE_URL=https://api.openai-proxy.org/anthropic \
claude --print "say hi"
```

而项目 `.env` 当时配置的是：

```text
ANTHROPIC_BASE_URL=http://127.0.0.1:18080
OPENAI_BASE_URL=https://api.openai-proxy.org/v1
```

这表示项目在走 **本地 `claude_proxy` -> 上游 OpenAI-compatible `/v1`** 这条链路，和上面直接走 `/anthropic` 兼容端点不是一回事。

如果第三方只对 `Anthropic-compatible /anthropic` 端点放行，而不接受经本地 proxy 转成 OpenAI 协议后的 `/v1` 调用，就会出现：

```text
401 Invalid API key
```

接设置：

```text
ANTHROPIC_BASE_URL=<第三方的 /anthropic 兼容地址>
ANTHROPIC_API_KEY=<对应 key>
```

- 不要再额外绕一层 `127.0.0.1:18080` 本地 proxy
- `OPENAI_BASE_URL` 只在你明确要使用本地 `claude_proxy` 转 OpenAI-compatible 上游时才需要

也就是说：**“终端里 `claude` 能跑通”和“当前项目 `.env` 的 proxy 路由正确”是两件不同的事。**

### 7. report_generation MCP 去除 trading 残留依赖

后续又做了一轮轻量清理，让 `report_generation` 的 MCP 更像“独立 skill 的服务”，而不是“直接复制 trading 再改一点点”。

**修复点**：

- `.claude/skills/report_generation/scripts/mcp/report_generation_mcp.py`
  - 新增 report_generation 专属环境变量别名：
    - `REPORT_GENERATION_DB_PATH`
    - `REPORT_GENERATION_TRADING_RESULTS_ROOT`
  - 同时保留旧的兼容别名：
    - `TRADING_DB_PATH`
    - `TRADING_RESULTS_ROOT`
  - 这样新代码语义更清晰，但旧调用方式也不会断。

- `claude_agent_trading/report_generation_daily.py`
  - MCP 探针跳过开关新增 `REPORT_GENERATION_MCP_SKIP_PROBE=1`
  - 同时继续兼容旧的 `TRADING_MCP_SKIP_PROBE=1`

- `claude_agent_trading/benchmark.py`
  - `report_generation` 的 prompt 文案改为和 skill 保持一致的 `equity research report`
  - 输出路径格式统一转成 POSIX 风格

**设计结论**：

`report_generation_mcp` 的骨架确实来自 `trading_mcp`，但现在它不是“仅复制 + 新增一个 `get_trading_decisions()`”这么简单，而是：

- 继承了相同的离线 DuckDB 访问模式
- 保留了适合报告生成的细粒度 list/get 工具（新闻、财报章节）
- 增加了报告专用的 `get_trading_decisions()` 作为跨 skill 桥接
- 去掉了配置层面对 `trading` skill 名字的强绑定，语义上变成独立 skill

---

## 会议后修正（独立 task 版本）

后续和需求方确认后，`report_generation` 与 `trading` **不是串联 task**，而是彼此独立的 benchmark task。

因此又做了一轮回退式清理，把此前引入的跨 skill 依赖移除：

- 删除 `report_generation_mcp` 中的 `get_trading_decisions()`
- 删除 `report-generation` CLI 的 `--trading-results-root`
- 删除 `ReportGenerationDailyConfig.trading_results_root`
- 删除 `report_generation` skill 文档中所有“读取 trading BUY/SELL/HOLD 信号”的流程说明

修正后的结论是：

- `trading`：独立输出交易决策 JSON
- `report_generation`：独立基于 DuckDB 中的价格 / 新闻 / 财报 / 技术指标生成报告
- 二者共享底层数据源和执行框架风格，但**不互相依赖结果文件**

也就是说，当前正确的设计不是：

```text
trading -> report_generation
```

而是：

```text
trading
report_generation
```

二者平行、独立执行。

---

## 后续优化：report_evaluation 也接入 MCP

进一步统一 benchmark 风格后，又把 `report_evaluation` 从“直接读 parquet + 直接扫报告目录”的模式，调整成与 `report_generation` 同一类的 **MCP + DuckDB + 脚本写出** 模式。

### 核心改动

- 新增 `report_evaluation_mcp`
  - 文件：`.claude/skills/report_evaluation/scripts/mcp/report_evaluation_mcp.py`
  - 注册：`.claude/skills/report_evaluation/.mcp.json`

- `report_evaluation_mcp` 提供两类工具：
  - **DuckDB 市场数据工具**：`get_prices`、`list_news`、`get_news_by_id`、`list_filings`、`get_filing_section`、`get_indicator`
  - **报告文件工具**：`list_reports`、`get_report_content`

- 新增写结果脚本：
  - `.claude/skills/report_evaluation/scripts/upsert_evaluation.py`

- benchmark 层新增对 `report_evaluation` 的 MCP 注入：
  - `claude_agent_trading/benchmark.py`
  - `claude_agent_trading/benchmark_cli.py`

### 接口变化

`report-evaluation` 不再以 parquet 目录为主输入，而改成：

```bash
python run_benchmark.py report-evaluation \
    --benchmark-root /path/to/benchmark \
    --ticker TSLA \
    --target-agent codex \
    --target-model gpt-5 \
    --db-path /path/to/trading_env.duckdb \
    --reports-root /path/to/results/report_generation \
    --output-root /path/to/results/report_evaluation
```

### 为什么这样改

- 让 `report_generation` 和 `report_evaluation` 在工程结构上保持一致
- 统一离线数据访问路径：都经由 MCP 读取 DuckDB，而不是一部分读 DuckDB、一部分读 parquet
- 减少 agent 运行时到处自己写脚本扫目录、读文件、读 parquet 的不稳定性
- 让 benchmark 更接近“标准化 task + 标准化工具”的目标

---

## skills 文档清理与统一

后续又对两个 skill 的 `SKILL.md` 做了一轮整理，目的不是改业务逻辑，而是让 **文档层和当前实现完全一致**，避免 agent 被旧说明或乱码误导。

### 1. `report_generation/SKILL.md` 重写

重写后的版本明确了：

- `report_generation` 是 **独立 task**，不依赖 `trading` 输出
- 数据访问统一通过 `report_generation_mcp`
- 结果写入统一通过 `upsert_report.py`
- 输出命名采用当前实际实现：

```text
{output_root}/{agent}_report_generation_{symbol}_{model}.json
{output_root}/{agent}_report_generation_{symbol}_{model}/{agent}_report_generation_{symbol}_{YYYYMMDD}_{model}.md
```

同时删除了旧版文档中残留的：

- `trading` 结果耦合描述
- 错乱编号的步骤说明
- 与当前脚本不一致的旧输出路径示例
- 编码损坏后的乱码字符

### 2. `report_evaluation/SKILL.md` 重写

重写后的版本明确了：

- `report_evaluation` 使用 `report_evaluation_mcp`
- 报告文件通过 MCP 工具 `list_reports` / `get_report_content` 读取
- 市场数据通过 DuckDB + MCP 获取
- 最终结果通过 `upsert_evaluation.py` 写出

也就是说，`report_evaluation` 不再以“直接读 parquet + 直接扫目录 + 大段临时 Python”为主叙述，而是和 `report_generation` 保持统一风格：

```text
DuckDB -> MCP -> Agent -> upsert script
```

### 3. 为什么这一步重要

`SKILL.md` 虽然不是项目直接 import 的 Python 源码，但它是 agent 的任务规范和操作说明。

如果 skill 文档：

- 还保留旧版流程
- 和真实实现不一致
- 混有编码乱码（如 `鈥`、`�`）

那么 agent 在运行时就可能：

- 走错数据访问路径
- 继续尝试旧的 parquet 逻辑
- 写出和当前脚本不兼容的结果

因此这轮清理的意义是：**把“实现正确”进一步推进到“agent 看到的说明也正确”。**
