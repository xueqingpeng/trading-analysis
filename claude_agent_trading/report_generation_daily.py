"""Daily-loop orchestrator for the single-day `report_generation` skill + MCP.

The heavy lifting lives in `<project_root>/.claude/skills/report_generation/SKILL.md`
and the `report_generation_mcp` MCP server (spawned by Claude CLI via `.mcp.json`).
This module just loops over dates and invokes one agent per day. The daily prompt
tells the agent to pass `--output-root=<output_dir>` to the skill's
`upsert_report.py`, so the result is written directly to the user-specified directory.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .benchmark import DEFAULT_PROJECT_ROOT
from .core import AgentResult, run_agent
from .providers import resolve_model

logger = logging.getLogger("claude_agent_framework")


@dataclass(slots=True)
class ReportGenerationDailyConfig:
    """Config for a date-range report-generation run driven day-by-day."""

    symbol: str
    start: date
    end: date
    output_dir: Path
    db_path: Path
    project_root: Path = field(default_factory=lambda: DEFAULT_PROJECT_ROOT.resolve())
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 1.0
    skip_weekends: bool = True
    fail_fast: bool = False


@dataclass(slots=True)
class DailyReportResult:
    date: str
    agent_result: AgentResult
    output_path: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "result": self.agent_result.result,
            "cost_usd": self.agent_result.cost_usd,
            "turns": self.agent_result.turns,
            "duration_ms": self.agent_result.duration_ms,
            "session_id": self.agent_result.session_id,
            "is_error": self.agent_result.is_error,
            "output_path": str(self.output_path) if self.output_path else None,
        }


@dataclass(slots=True)
class ReportGenerationRangeResult:
    config: ReportGenerationDailyConfig
    per_day: list[DailyReportResult]
    total_cost_usd: float
    num_errors: int

    def to_dict(self) -> dict[str, Any]:
        cfg = asdict(self.config)
        cfg["start"] = self.config.start.isoformat()
        cfg["end"] = self.config.end.isoformat()
        cfg["output_dir"] = str(self.config.output_dir)
        cfg["db_path"] = str(self.config.db_path)
        cfg["project_root"] = str(self.config.project_root)
        return {
            "config": cfg,
            "per_day": [d.to_dict() for d in self.per_day],
            "total_cost_usd": self.total_cost_usd,
            "num_errors": self.num_errors,
            "num_days": len(self.per_day),
        }


def build_daily_prompt(
    model: str, symbol: str, target_date: str, output_dir: Path
) -> str:
    """Build the single-day report-generation prompt sent to the agent."""
    return (
        f"you are {model}. Generate equity research report for {symbol} on {target_date}. "
        f"When calling upsert_report.py, pass --output-root={output_dir.as_posix()}."
    )


def iter_trading_days(
    start: date, end: date, *, skip_weekends: bool
) -> Iterator[date]:
    """Yield each calendar day in [start, end]. Skip Sat/Sun if skip_weekends."""
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    cur = start
    while cur <= end:
        if not (skip_weekends and cur.weekday() >= 5):
            yield cur
        cur += timedelta(days=1)


def run_report_generation_range(
    config: ReportGenerationDailyConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_day_start: Callable[[str], None] | None = None,
    on_day_complete: Callable[[DailyReportResult], None] | None = None,
) -> ReportGenerationRangeResult:
    """Run the report_generation skill once per day over [config.start, config.end]."""
    _precheck(config)
    db_path_abs = config.db_path.expanduser().resolve()
    mcp_servers = _load_mcp_servers(config.project_root, db_path_abs)
    resolved_model = config.model or resolve_model()
    output_dir_abs = config.output_dir.resolve()

    per_day: list[DailyReportResult] = []
    total_cost = 0.0
    num_errors = 0

    for d in iter_trading_days(
        config.start, config.end, skip_weekends=config.skip_weekends
    ):
        target_date = d.isoformat()
        if on_day_start:
            on_day_start(target_date)

        prompt = build_daily_prompt(
            resolved_model, config.symbol, target_date, output_dir_abs
        )
        agent_result = run_agent(
            prompt=prompt,
            cwd=str(config.project_root),
            model=resolved_model,
            max_turns=config.max_turns,
            max_budget_usd=config.max_budget_usd,
            setting_sources=["project"],
            mcp_servers=mcp_servers,
            on_assistant_text=on_assistant_text,
            on_thinking=on_thinking,
            on_tool_use=on_tool_use,
            on_stderr=on_stderr,
        )

        output_path = _find_output_file(output_dir_abs, config.symbol)

        day_result = DailyReportResult(
            date=target_date, agent_result=agent_result, output_path=output_path
        )
        per_day.append(day_result)
        total_cost += agent_result.cost_usd
        if agent_result.is_error:
            num_errors += 1

        if on_day_complete:
            on_day_complete(day_result)

        if config.fail_fast and agent_result.is_error:
            logger.warning("fail-fast: stopping after error on %s", target_date)
            break

    return ReportGenerationRangeResult(
        config=config,
        per_day=per_day,
        total_cost_usd=total_cost,
        num_errors=num_errors,
    )


SKILL_DIR = Path(".claude") / "skills" / "report_generation"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "report_generation_mcp.py"


def _load_mcp_servers(project_root: Path, db_path: Path) -> dict:
    """Parse the skill-local .mcp.json and append --db-path to each server's args."""
    mcp_json = project_root / SKILL_MCP_JSON
    raw = json.loads(mcp_json.read_text())
    servers = raw.get("mcpServers") or {}
    if not servers:
        raise ValueError(f"No mcpServers defined in {mcp_json}")

    for spec in servers.values():
        args = list(spec.get("args") or [])
        args.append(f"--db-path={db_path}")
        spec["args"] = args
    return servers


def _precheck(config: ReportGenerationDailyConfig) -> None:
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(f"Report generation SKILL.md not found at {skill}.")

    mcp_json = root / SKILL_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(f".mcp.json not found at {mcp_json}.")

    _check_mcp_server_importable(root)

    db = config.db_path.expanduser().resolve()
    if not db.is_file():
        schema = root / SKILL_DIR / "scripts" / "mcp" / "schema.sql"
        raise FileNotFoundError(
            f"DuckDB file not found at {db}. "
            f"Build it with `duckdb {db} < {schema}` "
            f"and populate prices/news/filings."
        )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    probe = config.output_dir / ".writable_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise PermissionError(
            f"Output directory is not writable: {config.output_dir} ({exc})"
        ) from exc


def _check_mcp_server_importable(project_root: Path) -> None:
    import subprocess
    import sys

    script = project_root / SKILL_MCP_SCRIPT
    if not script.is_file():
        raise FileNotFoundError(f"MCP server script not found at {script}.")

    if (
        os.environ.get("REPORT_GENERATION_MCP_SKIP_PROBE") == "1"
        or os.environ.get("TRADING_MCP_SKIP_PROBE") == "1"
    ):
        return

    probe = (
        "import fastmcp, duckdb, pandas_ta, pydantic, numpy, pandas  # noqa: F401\n"
        "print('ok')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(
            f"Failed to probe MCP server Python environment: {exc}"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "MCP server dependencies are missing in the Python environment the "
            f"Claude CLI will use to spawn the server ({sys.executable}).\n"
            f"Probe stderr:\n{result.stderr.strip()}\n\n"
            f"Fix: pip install -r {project_root / 'requirements.txt'}"
        )


def _find_output_file(output_dir: Path, symbol: str) -> Path | None:
    """Return the latest `*_report_generation_{SYMBOL}_*.json` the agent wrote."""
    if not output_dir.is_dir():
        return None
    candidates = list(output_dir.glob(f"*_report_generation_{symbol}_*.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
