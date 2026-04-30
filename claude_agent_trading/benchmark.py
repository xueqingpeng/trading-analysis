"""Benchmark runner for financial_agentic_benchmark tasks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .core import AgentResult, run_agent

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRADING_TICKERS = {
    "AAPL",
    "ADBE",
    "AMZN",
    "BMRN",
    "CRM",
    "GOOGL",
    "META",
    "MSFT",
    "NVDA",
    "TSLA",
}


@dataclass(slots=True)
class BenchmarkTask:
    """Structured task definition for a benchmark run."""

    task_type: str
    benchmark_root: str | None = None
    model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    permission_mode: str = "bypassPermissions"
    setting_sources: list[str] | None = None
    data_root: str | None = None
    db_path: str | None = None
    output_root: str | None = None
    reports_root: str | None = None
    ticker: str | None = None
    target_agent: str | None = None
    target_model: str | None = None
    filing_name: str | None = None
    issue_time: str | None = None
    concept_id: str | None = None
    period: str | None = None
    case_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkTask":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"Unknown task fields: {', '.join(unknown)}")
        return cls(**data)


@dataclass(slots=True)
class BenchmarkRunResult:
    """Result of a single benchmark task."""

    task: BenchmarkTask
    prompt: str
    agent_result: AgentResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": asdict(self.task),
            "prompt": self.prompt,
            "result": self.agent_result.result,
            "cost_usd": self.agent_result.cost_usd,
            "turns": self.agent_result.turns,
            "duration_ms": self.agent_result.duration_ms,
            "session_id": self.agent_result.session_id,
            "is_error": self.agent_result.is_error,
        }


@dataclass(slots=True)
class BatchRunResult:
    """Result of a batch benchmark run."""

    tasks_file: str
    results: list[BenchmarkRunResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks_file": self.tasks_file,
            "num_tasks": len(self.results),
            "num_errors": sum(1 for r in self.results if r.agent_result.is_error),
            "results": [r.to_dict() for r in self.results],
        }


def run_benchmark_task(
    task: BenchmarkTask,
    *,
    on_assistant_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_stderr=None,
) -> BenchmarkRunResult:
    """Run a single structured benchmark task.

    The Agent SDK automatically discovers skills from .claude/skills/
    when setting_sources includes "project" and cwd is the project root.
    """
    benchmark_root = _resolve_benchmark_root(task.benchmark_root)
    project_root = DEFAULT_PROJECT_ROOT.resolve()
    prompt = _build_prompt(task, benchmark_root)
    mcp_servers = _load_task_mcp_servers(task, project_root, benchmark_root)

    agent_result = run_agent(
        prompt=prompt,
        cwd=str(project_root),
        model=task.model,
        max_turns=task.max_turns or 30,
        max_budget_usd=task.max_budget_usd or 5.0,
        permission_mode=task.permission_mode,
        setting_sources=task.setting_sources or ["project"],
        mcp_servers=mcp_servers,
        on_assistant_text=on_assistant_text,
        on_thinking=on_thinking,
        on_tool_use=on_tool_use,
        on_stderr=on_stderr,
    )

    return BenchmarkRunResult(
        task=task,
        prompt=prompt,
        agent_result=agent_result,
    )


def run_benchmark_batch(
    tasks: Iterable[BenchmarkTask],
    *,
    tasks_file: str,
    fail_fast: bool = False,
    on_assistant_text=None,
    on_thinking=None,
    on_tool_use=None,
    on_stderr=None,
) -> BatchRunResult:
    """Run tasks sequentially."""

    tasks = list(tasks)
    if not tasks:
        raise ValueError("Batch tasks list is empty")

    results: list[BenchmarkRunResult] = []
    for task in tasks:
        result = run_benchmark_task(
            task,
            on_assistant_text=on_assistant_text,
            on_thinking=on_thinking,
            on_tool_use=on_tool_use,
            on_stderr=on_stderr,
        )
        results.append(result)
        if fail_fast and result.agent_result.is_error:
            break

    return BatchRunResult(tasks_file=tasks_file, results=results)


def load_tasks_file(tasks_file: str | Path) -> list[BenchmarkTask]:
    """Load JSONL batch tasks from disk."""

    path = Path(tasks_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Tasks file does not exist: {path}")

    tasks: list[BenchmarkTask] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} of {path}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Expected object on line {line_no} of {path}")
            tasks.append(BenchmarkTask.from_dict(payload))
    return tasks


def _resolve_benchmark_root(value: str | None) -> Path:
    """Resolve the benchmark root directory. Required — no hardcoded default."""
    if not value:
        raise ValueError(
            "benchmark_root is required. Pass --benchmark-root or set it in the task definition. "
            "Example: --benchmark-root /path/to/financial_agentic_benchmark"
        )
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"benchmark_root does not exist or is not a directory: {path}")
    return path


def _normalize_task_type(task_type: str) -> str:
    return task_type.replace("-", "_").strip().lower()


def _build_prompt(task: BenchmarkTask, benchmark_root: Path) -> str:
    task_type = _normalize_task_type(task.task_type)
    if task_type == "trading":
        ticker = _validate_trading_ticker(task.ticker)
        data_root = _resolve_path(task.data_root, benchmark_root / "data" / "trading")
        output_root = _resolve_output_dir(task.output_root, benchmark_root / "results" / "trading")
        return (
            f"Please make trading decision for {ticker}. "
            f"The input data is at {data_root}, please save the output json to {output_root}."
        )

    if task_type == "report_generation":
        ticker = _validate_trading_ticker(task.ticker)
        output_root = _resolve_output_dir(
            task.output_root,
            benchmark_root / "results" / "report_generation",
        )
        model = task.model or "claude-code"
        return (
            f"you are {model}. "
            f"Generate equity research report for {ticker}. "
            f"When calling upsert_report.py, pass --output-root={Path(output_root).as_posix()}."
        )

    if task_type == "report_evaluation":
        ticker = _validate_trading_ticker(task.ticker)
        target_agent = _required_value(task.target_agent, "target_agent")
        target_model = _required_value(task.target_model, "target_model")
        db_path = _required_path(task.db_path, "db_path")
        reports_root = _resolve_path(
            task.reports_root,
            benchmark_root / "results" / "report_generation",
        )
        output_root = _resolve_output_dir(
            task.output_root,
            benchmark_root / "results" / "report_evaluation",
        )
        return (
            f"Evaluate the {target_agent}/{ticker}/{target_model} run. "
            f"Reports parent: {reports_root}. "
            f"DuckDB: {db_path}. "
            f"Output: {output_root}."
        )

    if task_type == "auditing":
        filing_name = _required_value(task.filing_name, "filing_name").lower()
        if filing_name not in {"10k", "10q"}:
            raise ValueError("filing_name must be either '10k' or '10q'")
        ticker = _required_value(task.ticker, "ticker").lower()
        issue_time = _required_value(task.issue_time, "issue_time")
        if len(issue_time) != 8 or not issue_time.isdigit():
            raise ValueError("issue_time must be an 8-digit YYYYMMDD string")
        concept_id = _required_value(task.concept_id, "concept_id")
        period = _required_value(task.period, "period")
        case_id = _required_value(task.case_id, "case_id")
        data_root = _resolve_path(task.data_root, benchmark_root / "data" / "auditing")
        output_root = _resolve_output_dir(task.output_root, benchmark_root / "results" / "auditing")
        issue_date = datetime.strptime(issue_time, "%Y%m%d").strftime("%Y-%m-%d")
        return (
            f"Please audit the value of {concept_id} for {period} in the {filing_name} filing "
            f"released by {ticker} on {issue_date}. What's the reported value? What's the actual "
            f"value calculated from the relevant linkbases and US-GAAP taxonomy? (id: {case_id}) "
            f"The input data is at {data_root}, please save the output to {output_root}."
        )

    raise ValueError(
        f"Unsupported task_type '{task.task_type}'. "
        f"Expected one of: trading, report_generation, report_evaluation, auditing"
    )


def _validate_trading_ticker(ticker: str | None) -> str:
    value = _required_value(ticker, "ticker").upper()
    if value not in TRADING_TICKERS:
        raise ValueError(
            f"ticker must be one of: {', '.join(sorted(TRADING_TICKERS))}"
        )
    return value


def _required_value(value: str | None, field_name: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required")
    return str(value).strip()


def _resolve_path(value: str | None, default: Path) -> str:
    path = Path(value).expanduser().resolve() if value else default.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return str(path)


def _resolve_output_dir(value: str | None, default: Path) -> str:
    path = Path(value).expanduser().resolve() if value else default.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


REPORT_EVALUATION_SKILL_DIR = Path(".claude") / "skills" / "report_evaluation"
REPORT_EVALUATION_MCP_JSON = REPORT_EVALUATION_SKILL_DIR / ".mcp.json"


def _required_path(value: str | None, field_name: str) -> str:
    if not value:
        raise ValueError(f"{field_name} is required")
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return str(path)


def _load_task_mcp_servers(
    task: BenchmarkTask, project_root: Path, benchmark_root: Path
) -> dict | None:
    task_type = _normalize_task_type(task.task_type)
    if task_type != "report_evaluation":
        return None

    db_path = Path(_required_path(task.db_path, "db_path"))
    reports_root = Path(
        _resolve_path(task.reports_root, benchmark_root / "results" / "report_generation")
    )
    mcp_json = project_root / REPORT_EVALUATION_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(f".mcp.json not found at {mcp_json}.")

    raw = json.loads(mcp_json.read_text())
    servers = raw.get("mcpServers") or {}
    if not servers:
        raise ValueError(f"No mcpServers defined in {mcp_json}")

    for spec in servers.values():
        args = list(spec.get("args") or [])
        args.append(f"--db-path={db_path}")
        args.append(f"--reports-root={reports_root}")
        spec["args"] = args
    return servers
