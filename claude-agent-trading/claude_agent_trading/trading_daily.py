"""Daily-loop orchestrator for the single-day `trading` skill + MCP.

The heavy lifting lives in `<project_root>/.claude/skills/trading/SKILL.md` and
the `trading_mcp` MCP server (spawned by Claude CLI via `.mcp.json`). This
module just loops over dates, invokes one agent per day, and copies the
skill's output JSON to the user-specified `--output` directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .benchmark import DEFAULT_PROJECT_ROOT
from .core import AgentResult, run_agent

logger = logging.getLogger("claude_agent_framework")


@dataclass(slots=True)
class TradingDailyConfig:
    """Config for a date-range trading run driven day-by-day."""

    symbol: str
    start: date
    end: date
    output_dir: Path
    project_root: Path = field(default_factory=lambda: DEFAULT_PROJECT_ROOT.resolve())
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 1.0
    skip_weekends: bool = True
    fail_fast: bool = False


@dataclass(slots=True)
class DailyResult:
    date: str
    agent_result: AgentResult
    copied_to: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "result": self.agent_result.result,
            "cost_usd": self.agent_result.cost_usd,
            "turns": self.agent_result.turns,
            "duration_ms": self.agent_result.duration_ms,
            "session_id": self.agent_result.session_id,
            "is_error": self.agent_result.is_error,
            "copied_to": str(self.copied_to) if self.copied_to else None,
        }


@dataclass(slots=True)
class TradingRangeResult:
    config: TradingDailyConfig
    per_day: list[DailyResult]
    total_cost_usd: float
    num_errors: int

    def to_dict(self) -> dict[str, Any]:
        cfg = asdict(self.config)
        cfg["start"] = self.config.start.isoformat()
        cfg["end"] = self.config.end.isoformat()
        cfg["output_dir"] = str(self.config.output_dir)
        cfg["project_root"] = str(self.config.project_root)
        return {
            "config": cfg,
            "per_day": [d.to_dict() for d in self.per_day],
            "total_cost_usd": self.total_cost_usd,
            "num_errors": self.num_errors,
            "num_days": len(self.per_day),
        }


def build_daily_prompt(model: str, symbol: str, target_date: str) -> str:
    """Phrase matching the trading SKILL.md's 'trade AAPL on 2025-03-05' example."""
    return f"you are {model}. Trade {symbol} on {target_date}"


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


def run_trading_range(
    config: TradingDailyConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_day_start: Callable[[str], None] | None = None,
    on_day_complete: Callable[[DailyResult], None] | None = None,
) -> TradingRangeResult:
    """Run the trading skill once per day over [config.start, config.end].

    The skill upserts into `<project_root>/results/trading/trading_{SYMBOL}_*.json`.
    After each day we copy that file to `config.output_dir` for the caller.
    """
    _precheck(config)
    mcp_servers = _load_mcp_servers(config.project_root)

    per_day: list[DailyResult] = []
    total_cost = 0.0
    num_errors = 0

    for d in iter_trading_days(
        config.start, config.end, skip_weekends=config.skip_weekends
    ):
        target_date = d.isoformat()
        if on_day_start:
            on_day_start(target_date)

        prompt = build_daily_prompt(config.symbol, target_date)
        agent_result = run_agent(
            prompt=prompt,
            cwd=str(config.project_root),
            model=config.model,
            max_turns=config.max_turns,
            max_budget_usd=config.max_budget_usd,
            setting_sources=["project"],
            mcp_servers=mcp_servers,
            on_assistant_text=on_assistant_text,
            on_thinking=on_thinking,
            on_tool_use=on_tool_use,
            on_stderr=on_stderr,
        )

        copied = _copy_skill_output(
            config.project_root, config.symbol, config.output_dir
        )

        day_result = DailyResult(
            date=target_date, agent_result=agent_result, copied_to=copied
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

    return TradingRangeResult(
        config=config,
        per_day=per_day,
        total_cost_usd=total_cost,
        num_errors=num_errors,
    )


SKILL_DIR = Path(".claude") / "skills" / "trading"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "trading_mcp.py"
SKILL_DEFAULT_DB = SKILL_DIR / "scripts" / "env" / "trading_env.duckdb"


def _load_mcp_servers(project_root: Path) -> dict:
    """Parse the skill-local .mcp.json into the dict `mcp_servers` wants.

    The Agent SDK does NOT auto-read .mcp.json (that's a Claude Code CLI
    convention), so it doesn't care where the file lives — we parse it
    ourselves. We put it under `.claude/skills/trading/` to keep the whole
    skill (SKILL.md + MCP config + server script + DuckDB) self-contained.
    Relative `args` paths in it stay relative to cwd (= project_root).
    """
    mcp_json = project_root / SKILL_MCP_JSON
    raw = json.loads(mcp_json.read_text())
    servers = raw.get("mcpServers") or {}
    if not servers:
        raise ValueError(f"No mcpServers defined in {mcp_json}")
    return servers


def _precheck(config: TradingDailyConfig) -> None:
    """Fail fast with clear messages before spending any API cost."""
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(
            f"Trading SKILL.md not found at {skill}."
        )

    mcp_json = root / SKILL_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(
            f".mcp.json not found at {mcp_json}."
        )

    _check_mcp_server_importable(root)

    # trading_mcp.py resolves its default DuckDB path relative to the script's
    # own location: parent.parent / "env" / "trading_env.duckdb". With the
    # script at .claude/skills/trading/scripts/mcp/trading_mcp.py that becomes
    # .claude/skills/trading/scripts/env/trading_env.duckdb. TRADING_DB_PATH
    # overrides this.
    if not os.environ.get("TRADING_DB_PATH"):
        db = root / SKILL_DEFAULT_DB
        if not db.is_file():
            schema = root / SKILL_DIR / "scripts" / "mcp" / "schema.sql"
            raise FileNotFoundError(
                f"DuckDB file not found at {db}. "
                f"Build it with `duckdb {db} < {schema}` "
                f"and populate prices/news/filings, or set TRADING_DB_PATH."
            )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    # Touch-test writability so we fail before burning API cost.
    probe = config.output_dir / ".writable_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise PermissionError(
            f"Output directory is not writable: {config.output_dir} ({exc})"
        ) from exc


def _check_mcp_server_importable(project_root: Path) -> None:
    """Verify the MCP server script can import all deps before the SDK spawns it.

    Claude CLI swallows MCP startup errors silently — if fastmcp / duckdb /
    pandas_ta are missing, the agent sees zero tools and falls back to arbitrary
    Bash (incl. reading the DuckDB directly), which defeats the skill's contract.
    We catch that here so the user gets a clear `pip install` hint instead of
    a mysteriously empty tool set.
    """
    import subprocess
    import sys

    script = project_root / SKILL_MCP_SCRIPT
    if not script.is_file():
        raise FileNotFoundError(
            f"MCP server script not found at {script}."
        )

    # Escape hatch for tests / CI where the deps aren't installed
    if os.environ.get("TRADING_MCP_SKIP_PROBE") == "1":
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
        skill_reqs = project_root / SKILL_DIR / "scripts" / "mcp" / "requirements.txt"
        raise RuntimeError(
            "MCP server dependencies are missing in the Python environment the "
            f"Claude CLI will use to spawn the server ({sys.executable}).\n"
            f"Probe stderr:\n{result.stderr.strip()}\n\n"
            f"Fix: pip install -r {project_root / 'requirements.txt'}\n"
            f"(or -r {skill_reqs.relative_to(project_root)} for just the MCP deps)."
        )


def _copy_skill_output(
    project_root: Path, symbol: str, output_dir: Path
) -> Path | None:
    """Copy the latest `trading_{SYMBOL}_*.json` from the skill's results dir.

    Returns the destination path on success, or None if no file was found or
    the copy failed (we don't raise — a single day's copy failure shouldn't
    abort a multi-day run).
    """
    src_dir = project_root / "results" / "trading"
    if not src_dir.is_dir():
        return None

    candidates = list(src_dir.glob(f"trading_{symbol}_*.json"))
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    dst = output_dir / latest.name
    try:
        shutil.copy2(latest, dst)
    except OSError as exc:
        logger.warning("Failed to copy %s → %s: %s", latest, dst, exc)
        return None
    return dst
