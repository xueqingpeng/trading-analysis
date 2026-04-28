"""Daily-loop orchestrator for the single-day `hedging` skill + MCP.

Mirrors `trading_daily.py` but for the pair-trading hedging skill:

  * The first day in the range is the `IS_FIRST_DAY=True` day. The agent
    runs pair selection on that date, picks `(LEFT, RIGHT)`, writes the
    first record.
  * Subsequent days are `IS_FIRST_DAY=False` (the default). The agent
    reads the pair from the output file and only makes a daily decision.

The heavy lifting lives in `<project_root>/.claude/skills/hedging/SKILL.md`
and the `hedging_mcp` MCP server.
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
from .trading_daily import DailyResult

logger = logging.getLogger("claude_agent_framework")


@dataclass(slots=True)
class HedgingDailyConfig:
    """Config for a date-range hedging run driven day-by-day.

    The very first iterated date is the run's `IS_FIRST_DAY=True` date —
    that's when the agent runs pair selection. Every later date is
    `IS_FIRST_DAY=False` (the default), and the agent reads the pair from
    the existing output file.
    """

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
class HedgingRangeResult:
    config: HedgingDailyConfig
    per_day: list[DailyResult]
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
    model: str, target_date: str, output_dir: Path, is_first_day: bool
) -> str:
    """Build the single-day hedging prompt sent to the agent.

    Pins `IS_FIRST_DAY`, `--output-root`, and `--model` so the agent passes
    them verbatim. Everything else (pair selection rules, consistency check,
    no-look-ahead) is owned by `.claude/skills/hedging/SKILL.md`.
    """
    flag = "True" if is_first_day else "False"
    verb = "Start hedging on" if is_first_day else "Run hedging for"
    return (
        f"{verb} {target_date} with IS_FIRST_DAY={flag}.\n\n"
        f"Your turn is NOT complete unless you have actually invoked the "
        f"Bash tool to run `python3 .claude/skills/hedging/scripts/"
        f"upsert_hedging_decision.py` with all required flags. A text-only "
        f"response that merely describes or announces the decision is a "
        f"FAILURE — the result file will not exist on disk. Do not stop, "
        f"do not write a summary, do not say the decision has been recorded "
        f"until the Bash call has returned its one-line JSON success "
        f"summary.\n\n"
        f"When calling upsert_hedging_decision.py, pass "
        f"--output-root={output_dir} and --model={model} exactly as given "
        f"(do not substitute your own model name)."
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


def run_hedging_range(
    config: HedgingDailyConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_day_start: Callable[[str], None] | None = None,
    on_day_complete: Callable[[DailyResult], None] | None = None,
) -> HedgingRangeResult:
    """Run the hedging skill once per day over [config.start, config.end].

    The first iterated trading day in the range is treated as
    `IS_FIRST_DAY=True` (pair selection happens that day). Every later day
    is `IS_FIRST_DAY=False`.
    """
    _precheck(config)
    db_path_abs = config.db_path.expanduser().resolve()
    mcp_servers = _load_mcp_servers(config.project_root, db_path_abs)
    resolved_model = config.model or resolve_model()
    output_dir_abs = config.output_dir.resolve()

    # Detect resumption: if the output directory already has a hedging_*.json
    # file, this is a continuation of a previous run — the pair was already
    # selected, so every iterated date uses IS_FIRST_DAY=False. Only when the
    # directory is fresh do we fire IS_FIRST_DAY=True on the very first day.
    new_run = _find_output_file(output_dir_abs) is None
    if new_run:
        logger.info("output dir is empty → first iterated day will be IS_FIRST_DAY=True")
    else:
        logger.info("found existing hedging_*.json → resuming run, all days IS_FIRST_DAY=False")

    per_day: list[DailyResult] = []
    total_cost = 0.0
    num_errors = 0
    first_day_seen = False

    for d in iter_trading_days(
        config.start, config.end, skip_weekends=config.skip_weekends
    ):
        target_date = d.isoformat()
        is_first_day = new_run and not first_day_seen
        first_day_seen = True

        if on_day_start:
            on_day_start(target_date)

        prompt = build_daily_prompt(
            resolved_model, target_date, output_dir_abs, is_first_day
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

        output_path = _find_output_file(output_dir_abs)

        day_result = DailyResult(
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

    return HedgingRangeResult(
        config=config,
        per_day=per_day,
        total_cost_usd=total_cost,
        num_errors=num_errors,
    )


SKILL_DIR = Path(".claude") / "skills" / "hedging"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "hedging_mcp.py"


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


def _precheck(config: HedgingDailyConfig) -> None:
    """Fail fast with clear messages before spending any API cost."""
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(
            f"Hedging SKILL.md not found at {skill}."
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

    if os.environ.get("HEDGING_MCP_SKIP_PROBE") == "1":
        return

    probe = (
        "import fastmcp, duckdb, pydantic, numpy, pandas  # noqa: F401\n"
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
            "Hedging MCP server dependencies are missing in the Python "
            f"environment the Claude CLI will use to spawn the server "
            f"({sys.executable}).\n"
            f"Probe stderr:\n{result.stderr.strip()}\n\n"
            f"Fix: pip install -r {project_root / 'requirements.txt'}"
        )


def _find_output_file(output_dir: Path) -> Path | None:
    """Return the latest `hedging_*.json` the agent wrote in output_dir.

    Caller doesn't know `(LEFT, RIGHT)` in advance — pair selection picks it
    on the first day. So we glob `hedging_*.json` and take the freshest.
    Returns None if the agent didn't produce one.
    """
    if not output_dir.is_dir():
        return None
    candidates = list(output_dir.glob("hedging_*.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
