"""Daily-loop orchestrator for the single-day `trading` skill + MCP.

The heavy lifting lives in `<project_root>/.claude/skills/trading/SKILL.md` and
the `trading_mcp` MCP server (spawned by Claude CLI via `.mcp.json`). This
module just loops over dates and invokes one agent per day. The daily prompt
tells the agent to pass `--output-root=<output_dir>` to the skill's
`upsert_decision.py`, so the result JSON is written directly to the
user-specified directory.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .benchmark import DEFAULT_PROJECT_ROOT
from .core import AgentResult, run_agent
from .providers import resolve_model

logger = logging.getLogger("claude_agent_framework")


@dataclass(slots=True)
class TradingDailyConfig:
    """Config for a date-range trading run driven day-by-day."""

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
class DailyResult:
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
    """Build the single-day trading prompt sent to the agent.

    Embeds the caller-supplied `output_dir` and `model` so the agent passes
    `--output-root` and `--model` verbatim to `upsert_decision.py`. Pinning
    `--model` here prevents the agent from self-identifying (often wrongly
    on third-party providers) or inheriting a stale model name from a
    leftover output file in `output_dir`.
    """
    return (
        f"Trade {symbol} on {target_date}.\n\n"
        f"Your turn is NOT complete unless you have actually invoked the "
        f"Bash tool to run `python .claude/skills/trading/scripts/"
        f"upsert_decision.py` with all required flags. A text-only response "
        f"that merely describes or announces the decision is a FAILURE — "
        f"the result file will not exist on disk. Do not stop, do not write "
        f"a summary, do not say the decision has been recorded until the "
        f"Bash call has returned its one-line JSON success summary.\n\n"
        f"When calling upsert_decision.py, pass --output-root={output_dir} "
        f"and --model={model} exactly as given (do not substitute your own "
        f"model name)."
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

    The daily prompt tells the agent to pass `--output-root=<output_dir>`
    to `upsert_decision.py`, so the skill writes `trading_{SYMBOL}_*.json`
    straight into `config.output_dir`.
    """
    _precheck(config)
    db_path_abs = config.db_path.expanduser().resolve()
    mcp_servers = _load_mcp_servers(config.project_root, db_path_abs)
    resolved_model = config.model or resolve_model()
    output_dir_abs = config.output_dir.resolve()
    output_dir_abs.mkdir(parents=True, exist_ok=True)

    # Resume: load dates already written to disk so we can skip them.
    completed_dates = _load_completed_dates(output_dir_abs, config.symbol)
    if completed_dates:
        logger.info(
            "resume: %d date(s) already in output file — skipping them",
            len(completed_dates),
        )

    # Run log: append every day's outcome to a persistent log file.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = output_dir_abs / f"run_{config.symbol}_{ts}.log"
    log_file = log_path.open("w", buffering=1, encoding="utf-8")
    log_file.write(
        f"# trading run  symbol={config.symbol}  "
        f"start={config.start}  end={config.end}  model={resolved_model}\n"
        f"# started {datetime.now(timezone.utc).isoformat()}\n\n"
    )

    per_day: list[DailyResult] = []
    total_cost = 0.0
    num_errors = 0

    for d in iter_trading_days(
        config.start, config.end, skip_weekends=config.skip_weekends
    ):
        target_date = d.isoformat()

        # Auto-resume: skip dates already successfully recorded.
        if target_date in completed_dates:
            logger.info("[day] %s SKIP (resume)", target_date)
            log_file.write(f"[day] {target_date}  SKIP (resume)\n")
            continue

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

        day_result = DailyResult(
            date=target_date, agent_result=agent_result, output_path=output_path
        )
        per_day.append(day_result)
        total_cost += agent_result.cost_usd
        if agent_result.is_error:
            num_errors += 1
            # On success, add to completed so a same-process retry won't re-run.
        else:
            completed_dates.add(target_date)

        log_file.write(
            f"[day] {target_date}  "
            f"{'ERROR' if agent_result.is_error else 'OK'}  "
            f"cost=${agent_result.cost_usd:.4f}  turns={agent_result.turns}\n"
        )
        log_file.flush()

        if on_day_complete:
            on_day_complete(day_result)

        if config.fail_fast and agent_result.is_error:
            logger.warning("fail-fast: stopping after error on %s", target_date)
            break

    log_file.write(
        f"\n# finished {datetime.now(timezone.utc).isoformat()}  "
        f"total_cost=${total_cost:.4f}  errors={num_errors}\n"
    )
    log_file.close()
    logger.info("run log written to %s", log_path)

    return TradingRangeResult(
        config=config,
        per_day=per_day,
        total_cost_usd=total_cost,
        num_errors=num_errors,
    )


SKILL_DIR = Path(".claude") / "skills" / "trading"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "trading_mcp.py"


def _load_mcp_servers(project_root: Path, db_path: Path) -> dict:
    """Parse the skill-local .mcp.json into the dict `mcp_servers` wants.

    The Agent SDK does NOT auto-read .mcp.json (that's a Claude Code CLI
    convention), so it doesn't care where the file lives — we parse it
    ourselves. We put it under `.claude/skills/trading/` to keep the whole
    skill (SKILL.md + MCP config + server script + DuckDB) self-contained.
    Relative `args` paths in it stay relative to cwd (= project_root).

    We append `--db-path=<abs>` to each server's args so the DuckDB location
    is explicit at spawn time instead of being baked into the MCP script.
    """
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

    db = config.db_path.expanduser().resolve()
    if not db.is_file():
        schema = root / SKILL_DIR / "scripts" / "mcp" / "schema.sql"
        raise FileNotFoundError(
            f"DuckDB file not found at {db}. "
            f"Build it with `duckdb {db} < {schema}` "
            f"and populate prices/news/filings."
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
            timeout=60,
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


def _load_completed_dates(output_dir: Path, symbol: str) -> set[str]:
    """Return the set of dates already recorded in the output JSON."""
    candidates = list(output_dir.glob(f"trading_{symbol}_*.json")) if output_dir.is_dir() else []
    if not candidates:
        return set()
    out_file = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        doc = json.loads(out_file.read_text())
        return {r["date"] for r in doc.get("recommendations", []) if "date" in r}
    except Exception:
        return set()


def _find_output_file(output_dir: Path, symbol: str) -> Path | None:
    """Return the latest `trading_{SYMBOL}_*.json` the agent wrote in output_dir.

    The skill writes directly via `upsert_decision.py --output-root=<dir>`;
    this just picks the freshest matching file so callers can report where
    the result landed. Returns None if the agent didn't produce one.
    """
    if not output_dir.is_dir():
        return None
    candidates = list(output_dir.glob(f"trading_{symbol}_*.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
