"""Weekly-loop orchestrator for the single-week `report_generation` skill + MCP.

Mirrors `trading_daily.py` but iterates **Fridays** in `[start, end]`. The
report week is anchored on the Monday of TARGET_DATE's ISO calendar week
through TARGET_DATE inclusive, so a Friday TARGET_DATE captures Mon-Fri
(5 trading days), a Thursday fallback (Friday holiday) captures Mon-Thu,
and a Monday holiday simply drops Monday from the window. If a Friday is
a market holiday, the skill's `is_trading_day` falls back to the prior
trading day automatically.

The heavy lifting lives in `<project_root>/.claude/skills/report_generation/SKILL.md`
and the `report_generation_mcp` MCP server.
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
class ReportGenerationWeeklyConfig:
    """Config for a date-range report_generation run driven Friday-by-Friday."""

    symbol: str
    start: date
    end: date
    output_dir: Path
    db_path: Path
    project_root: Path = field(default_factory=lambda: DEFAULT_PROJECT_ROOT.resolve())
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 1.0
    fail_fast: bool = False


@dataclass(slots=True)
class WeeklyResult:
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
    config: ReportGenerationWeeklyConfig
    per_week: list[WeeklyResult]
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
            "per_week": [w.to_dict() for w in self.per_week],
            "total_cost_usd": self.total_cost_usd,
            "num_errors": self.num_errors,
            "num_weeks": len(self.per_week),
        }


def build_weekly_prompt(
    model: str,
    symbol: str,
    target_date: str,
    output_dir: Path,
) -> str:
    """Build the single-week report_generation prompt sent to the agent.

    Pins `--symbol`, `--target-date`, `--output-root`, and `--model` so the
    agent passes them verbatim to `upsert_report.py`. The agent still
    decides `--action` (the rating) and `--price` (the week-close price
    from `get_weekly_metrics`) based on its analysis — those are taught
    by SKILL.md, not pinned here.
    """
    return (
        f"Generate the weekly equity research report for {symbol} for the "
        f"week ending {target_date}.\n\n"
        f"Your turn is NOT complete unless you have actually invoked the "
        f"Bash tool to run `python3 .claude/skills/report_generation/scripts/"
        f"upsert_report.py` with all required flags AND piped the full "
        f"Markdown report on stdin. A text-only response that merely "
        f"describes or announces the report is a FAILURE — the result file "
        f"will not exist on disk. Do not stop, do not write a summary, do "
        f"not say the report has been written until the Bash call has "
        f"returned its one-line JSON success summary.\n\n"
        f"When calling upsert_report.py, pass --symbol={symbol} "
        f"--target-date={target_date} "
        f"--output-root={output_dir} and --model={model} exactly as given "
        f"(do not substitute your own model name)."
    )


def iter_report_fridays(start: date, end: date) -> Iterator[date]:
    """Yield each Friday in [start, end] inclusive.

    If `start` is not a Friday, advance to the next Friday. Friday is
    `weekday() == 4` (Mon=0, Sun=6).
    """
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    days_ahead = (4 - start.weekday()) % 7  # 0 if start is already Friday
    cur = start + timedelta(days=days_ahead)
    while cur <= end:
        yield cur
        cur += timedelta(days=7)


def run_report_generation_range(
    config: ReportGenerationWeeklyConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_week_start: Callable[[str], None] | None = None,
    on_week_complete: Callable[[WeeklyResult], None] | None = None,
) -> ReportGenerationRangeResult:
    """Run the report_generation skill once per Friday in [config.start, config.end].

    The weekly prompt tells the agent to pass `--output-root=<output_dir>`
    to `upsert_report.py`, so the skill writes
    `report_generation_{SYMBOL}_{model}.json` and the per-week Markdown
    body straight into `config.output_dir`.
    """
    _precheck(config)
    db_path_abs = config.db_path.expanduser().resolve()
    mcp_servers = _load_mcp_servers(config.project_root, db_path_abs)
    resolved_model = config.model or resolve_model()
    output_dir_abs = config.output_dir.resolve()

    per_week: list[WeeklyResult] = []
    total_cost = 0.0
    num_errors = 0

    for d in iter_report_fridays(config.start, config.end):
        target_date = d.isoformat()
        if on_week_start:
            on_week_start(target_date)

        prompt = build_weekly_prompt(
            resolved_model,
            config.symbol,
            target_date,
            output_dir_abs,
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

        week_result = WeeklyResult(
            date=target_date, agent_result=agent_result, output_path=output_path
        )
        per_week.append(week_result)
        total_cost += agent_result.cost_usd
        if agent_result.is_error:
            num_errors += 1

        if on_week_complete:
            on_week_complete(week_result)

        if config.fail_fast and agent_result.is_error:
            logger.warning("fail-fast: stopping after error on %s", target_date)
            break

    return ReportGenerationRangeResult(
        config=config,
        per_week=per_week,
        total_cost_usd=total_cost,
        num_errors=num_errors,
    )


SKILL_DIR = Path(".claude") / "skills" / "report_generation"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "report_generation_mcp.py"


def _load_mcp_servers(project_root: Path, db_path: Path) -> dict:
    """Parse the skill-local .mcp.json and inject the absolute db path."""
    mcp_json = project_root / SKILL_MCP_JSON
    raw = json.loads(mcp_json.read_text())
    servers = raw.get("mcpServers") or {}
    if not servers:
        raise ValueError(f"No mcpServers defined in {mcp_json}")

    db_arg = f"--db-path={db_path}"
    for spec in servers.values():
        args = list(spec.get("args") or [])
        args.append(db_arg)
        spec["args"] = args
    return servers


def _precheck(config: ReportGenerationWeeklyConfig) -> None:
    """Fail fast with clear messages before spending any API cost."""
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(
            f"Report-generation SKILL.md not found at {skill}."
        )

    mcp_json = root / SKILL_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(
            f".mcp.json not found at {mcp_json}."
        )

    _check_mcp_server_importable(root)

    db = config.db_path.expanduser().resolve()
    if not db.is_file():
        raise FileNotFoundError(
            f"DuckDB file not found at {db}. "
            f"Build it with the trading skill's schema "
            f"(.claude/skills/trading/scripts/mcp/schema.sql) and populate "
            f"prices/news/filings."
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
    """Verify the MCP server can import its deps before the SDK spawns it."""
    import subprocess
    import sys

    script = project_root / SKILL_MCP_SCRIPT
    if not script.is_file():
        raise FileNotFoundError(
            f"MCP server script not found at {script}."
        )

    if os.environ.get("REPORT_GENERATION_MCP_SKIP_PROBE") == "1":
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
            "Report-generation MCP server dependencies are missing in the "
            f"Python environment the Claude CLI will use to spawn the server "
            f"({sys.executable}).\n"
            f"Probe stderr:\n{result.stderr.strip()}\n\n"
            f"Fix: pip install -r {project_root / 'requirements.txt'}"
        )


def _find_output_file(output_dir: Path, symbol: str) -> Path | None:
    """Return the latest `report_generation_{SYMBOL}_*.json` summary
    the agent wrote in output_dir.

    The skill writes the summary JSON and per-week Markdown bodies via
    `upsert_report.py --output-root=<dir>`; this picks the freshest matching
    summary so callers can report where the result landed. Returns None if
    the agent didn't produce one.
    """
    if not output_dir.is_dir():
        return None
    pattern = f"report_generation_{symbol}_*.json"
    candidates = list(output_dir.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
