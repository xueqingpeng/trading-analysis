"""Single-shot orchestrator for the `auditing` skill + MCP.

Mirrors the structure of `trading_daily.py` / `hedging_daily.py`, but without
the daily loop — each invocation handles exactly one
(filing, concept, period) triple.

Owns its own:
  * `build_auditing_prompt` — the prompt sent to the agent
  * `_load_mcp_servers` — parses skill-local `.mcp.json` and injects
    `--data-root=<resolved path>`
  * `_precheck` — fails fast on missing skill / MCP deps / invalid paths
  * `_check_mcp_server_importable` — same dep-probe pattern as trading/hedging

so the auditing skill is fully self-contained at the runner layer instead of
falling back to the generic `BenchmarkTask` dispatcher.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tqdm import tqdm

from .benchmark import DEFAULT_PROJECT_ROOT
from .core import AgentResult, run_agent
from .providers import resolve_model

logger = logging.getLogger("claude_agent_framework")

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(value: str) -> str:
    """Mirror the sanitization in `write_audit.py` so the runner can locate
    the file the script will produce."""
    return _FILENAME_SAFE_RE.sub("-", value)


@dataclass(slots=True)
class AuditingConfig:
    """Config for a single auditing run.

    Mirrors the inputs in `.claude/skills/auditing/SKILL.md`. `data_root` and
    `output_root` are optional — when omitted they fall back to
    `<benchmark_root>/data/auditing` and `<benchmark_root>/results/auditing`.
    """

    filing_name: str       # "10k" or "10q" (lowercase)
    ticker: str            # lowercase ticker
    issue_time: str        # "YYYYMMDD"
    concept_id: str        # e.g. "us-gaap:AssetsCurrent"
    period: str            # e.g. "FY2023" or "2023-01-01 to 2023-12-31"
    case_id: str           # task identifier embedded in the prompt
    benchmark_root: Path | None = None  # used only to derive defaults for data/output_root
    data_root: Path | None = None
    output_root: Path | None = None
    project_root: Path = field(default_factory=lambda: DEFAULT_PROJECT_ROOT.resolve())
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 5.0


@dataclass(slots=True)
class AuditingResult:
    config: AuditingConfig
    prompt: str
    agent_result: AgentResult
    output_path: Path | None

    def to_dict(self) -> dict[str, Any]:
        cfg = asdict(self.config)
        cfg["benchmark_root"] = str(self.config.benchmark_root)
        cfg["data_root"] = str(self.config.data_root) if self.config.data_root else None
        cfg["output_root"] = str(self.config.output_root) if self.config.output_root else None
        cfg["project_root"] = str(self.config.project_root)
        return {
            "config": cfg,
            "prompt": self.prompt,
            "result": self.agent_result.result,
            "cost_usd": self.agent_result.cost_usd,
            "turns": self.agent_result.turns,
            "duration_ms": self.agent_result.duration_ms,
            "session_id": self.agent_result.session_id,
            "is_error": self.agent_result.is_error,
            "output_path": str(self.output_path) if self.output_path else None,
        }


def _audit_run_clauses(model: str, output_root: Path) -> str:
    """The boilerplate appended to every audit prompt.

    Pins `--model` here (rather than letting the agent self-identify) for the
    same reason `trading_daily.build_daily_prompt` does — third-party
    providers' agents often misreport their own model id, which would
    corrupt the `auditing_..._{model}.json` output filename and break
    benchmark traceability. Also forces an actual Bash invocation of
    `write_audit.py` so a text-only response cannot pass as a success.
    """
    return (
        "\n\nYour turn is NOT complete unless you have actually invoked the "
        "Bash tool to run `python .claude/skills/auditing/scripts/"
        "write_audit.py` with all required flags. A text-only response "
        "that merely describes the audit is a FAILURE — the result file "
        "will not exist on disk. Do not stop, do not write a summary, do "
        "not say the audit has been recorded until the Bash call has "
        "returned its one-line JSON success summary.\n\n"
        f"When calling write_audit.py, pass --output-root={output_root} "
        f"and --model={model} exactly as given (do not substitute your own "
        f"model name)."
    )


def build_auditing_prompt(
    model: str,
    filing_name: str,
    ticker: str,
    issue_time: str,
    concept_id: str,
    period: str,
    case_id: str,
    data_root: Path,
    output_root: Path,
) -> str:
    """Build the single-shot auditing prompt sent to the agent."""
    issue_date = datetime.strptime(issue_time, "%Y%m%d").strftime("%Y-%m-%d")
    body = (
        f"Please audit the value of {concept_id} for {period} in the "
        f"{filing_name} filing released by {ticker} on {issue_date}. "
        f"What's the reported value? What's the actual value calculated from "
        f"the relevant linkbases and US-GAAP taxonomy? "
        f"(id: {case_id}) "
        f"The input data is at {data_root}."
    )
    return body + _audit_run_clauses(model, output_root)


def run_auditing(
    config: AuditingConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> AuditingResult:
    """Run the auditing skill once for the given config.

    Owns the full lifecycle (precheck → MCP injection → prompt → agent →
    output detection). Does NOT route through `run_benchmark_task` — keeps
    the auditing flow self-contained, mirroring `run_trading_range` /
    `run_hedging_range`.
    """
    data_root, output_root = _resolve_paths(config)

    _precheck(config, data_root, output_root)

    mcp_servers = _load_mcp_servers(config.project_root, data_root)
    resolved_model = config.model or resolve_model()

    prompt = build_auditing_prompt(
        model=resolved_model,
        filing_name=config.filing_name,
        ticker=config.ticker,
        issue_time=config.issue_time,
        concept_id=config.concept_id,
        period=config.period,
        case_id=config.case_id,
        data_root=data_root,
        output_root=output_root,
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

    output_path = _find_output_file(output_root, config)

    return AuditingResult(
        config=config,
        prompt=prompt,
        agent_result=agent_result,
        output_path=output_path,
    )


SKILL_DIR = Path(".claude") / "skills" / "auditing"
SKILL_MCP_JSON = SKILL_DIR / ".mcp.json"
SKILL_MCP_SCRIPT = SKILL_DIR / "scripts" / "mcp" / "auditing_mcp.py"


def _resolve_paths(config: AuditingConfig) -> tuple[Path, Path]:
    """Resolve data_root / output_root, falling back to benchmark_root defaults.

    `benchmark_root` is only consulted when one of the two paths is missing.
    If neither defaults are needed (both paths given explicitly), it can be
    None.
    """
    data_root = config.data_root
    output_root = config.output_root
    if data_root is None or output_root is None:
        if config.benchmark_root is None:
            raise ValueError(
                "Provide either --benchmark-root, or both --data-root and "
                "--output-root explicitly."
            )
        if data_root is None:
            data_root = config.benchmark_root / "data" / "auditing"
        if output_root is None:
            output_root = config.benchmark_root / "results" / "auditing"
    return data_root.expanduser().resolve(), output_root.expanduser().resolve()


def _load_mcp_servers(project_root: Path, data_root: Path) -> dict:
    """Parse skill-local `.mcp.json` and inject `--data-root=<abs>` into args.

    Same pattern as `trading_daily._load_mcp_servers` / `hedging_daily._load_mcp_servers`.
    """
    mcp_json = project_root / SKILL_MCP_JSON
    raw = json.loads(mcp_json.read_text())
    servers = raw.get("mcpServers") or {}
    if not servers:
        raise ValueError(f"No mcpServers defined in {mcp_json}")

    data_arg = f"--data-root={data_root}"
    for spec in servers.values():
        args = list(spec.get("args") or [])
        args.append(data_arg)
        spec["args"] = args
    return servers


def _precheck(config: AuditingConfig, data_root: Path, output_root: Path) -> None:
    """Fail fast with clear messages before spending any API cost."""
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(f"Auditing SKILL.md not found at {skill}.")

    mcp_json = root / SKILL_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(f".mcp.json not found at {mcp_json}.")

    _check_mcp_server_importable(root)

    if config.benchmark_root is not None and not config.benchmark_root.is_dir():
        raise FileNotFoundError(
            f"benchmark_root does not exist or is not a directory: "
            f"{config.benchmark_root}"
        )

    if not data_root.is_dir():
        raise FileNotFoundError(
            f"Auditing data root does not exist: {data_root}. "
            f"Pass --data-root or populate <benchmark_root>/data/auditing."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    probe = output_root / ".writable_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise PermissionError(
            f"Output directory is not writable: {output_root} ({exc})"
        ) from exc


def _check_mcp_server_importable(project_root: Path) -> None:
    """Verify the MCP server can import its deps before the SDK spawns it.

    Same protective pattern as `trading_daily._check_mcp_server_importable` —
    Claude CLI swallows MCP startup errors silently, so we surface missing
    deps here with a clear `pip install` hint.
    """
    import subprocess
    import sys

    script = project_root / SKILL_MCP_SCRIPT
    if not script.is_file():
        raise FileNotFoundError(f"MCP server script not found at {script}.")

    if os.environ.get("AUDITING_MCP_SKIP_PROBE") == "1":
        return

    probe = (
        "import fastmcp, pydantic  # noqa: F401\n"
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
            "Auditing MCP server dependencies are missing in the Python "
            f"environment the Claude CLI will use to spawn the server "
            f"({sys.executable}).\n"
            f"Probe stderr:\n{result.stderr.strip()}\n\n"
            f"Fix: pip install -r {project_root / 'requirements.txt'}"
        )


# ----------------------------------------------------------------------
# Batch mode — many cases in one docker invocation
# ----------------------------------------------------------------------


@dataclass(slots=True)
class AuditingBatchConfig:
    """Config for running many auditing cases in one docker invocation.

    `tasks_file` is a plain text file with one prompt per line. Each prompt
    may contain `{env_dir}` / `{result_dir}` placeholders, which the runner
    substitutes with the resolved `data_root` / `output_root` before sending
    to the agent. The required `--output-root` / `--model` pinning clauses
    are appended automatically.

    `resume=True` (default): when a task's expected output file already
    exists in `output_root`, skip that task — useful for re-running an
    interrupted batch without paying for completed cases. Set False to
    re-run every task even if its output exists.
    """

    tasks_file: Path
    benchmark_root: Path | None = None  # used only to derive defaults for data/output_root
    data_root: Path | None = None
    output_root: Path | None = None
    project_root: Path = field(default_factory=lambda: DEFAULT_PROJECT_ROOT.resolve())
    model: str | None = None
    max_turns: int = 30
    max_budget_usd: float = 5.0
    fail_fast: bool = False
    resume: bool = True
    workers: int = 1


@dataclass(slots=True)
class AuditingTaskResult:
    case_id: str | None  # extracted from "(id: ...)" if present
    prompt: str
    agent_result: AgentResult | None  # None if task was skipped (resume hit)
    output_path: Path | None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        ar = self.agent_result
        return {
            "case_id": self.case_id,
            "skipped": self.skipped,
            "result": ar.result if ar else None,
            "cost_usd": ar.cost_usd if ar else 0.0,
            "turns": ar.turns if ar else 0,
            "duration_ms": ar.duration_ms if ar else 0,
            "session_id": ar.session_id if ar else "",
            "is_error": ar.is_error if ar else False,
            "output_path": str(self.output_path) if self.output_path else None,
        }


@dataclass(slots=True)
class AuditingBatchResult:
    config: AuditingBatchConfig
    per_task: list[AuditingTaskResult]
    total_cost_usd: float
    num_errors: int
    num_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        cfg = asdict(self.config)
        cfg["tasks_file"] = str(self.config.tasks_file)
        cfg["benchmark_root"] = str(self.config.benchmark_root)
        cfg["data_root"] = str(self.config.data_root) if self.config.data_root else None
        cfg["output_root"] = str(self.config.output_root) if self.config.output_root else None
        cfg["project_root"] = str(self.config.project_root)
        return {
            "config": cfg,
            "per_task": [t.to_dict() for t in self.per_task],
            "total_cost_usd": self.total_cost_usd,
            "num_errors": self.num_errors,
            "num_skipped": self.num_skipped,
            "num_tasks": len(self.per_task),
        }


_CASE_ID_RE = re.compile(r"\(id:\s*([^)]+)\)")

# Parses the audit-prompt body the user produced in auditing.txt:
#   "Please audit the value of <CONCEPT> for <PERIOD> in the <FILING>
#    filing released by <TICKER> on <YYYY-MM-DD>."
# All five fields are needed to predict the filename `write_audit.py` will
# create, which is what `resume=True` keys off of.
_AUDIT_PROMPT_RE = re.compile(
    r"Please audit the value of (?P<concept>\S+)"
    r" for (?P<period>.+?)"
    r" in the (?P<filing>10[kq]) filing"
    r" released by (?P<ticker>\S+)"
    r" on (?P<issue_date>\d{4}-\d{2}-\d{2})\."
)


def _parse_audit_prompt(prompt: str) -> dict[str, str] | None:
    """Extract the 5 fields needed to predict the audit output filename.

    Returns None if the prompt doesn't match the expected shape (in which
    case resume can't decide and we fall through to running the task).
    """
    m = _AUDIT_PROMPT_RE.search(prompt)
    if not m:
        return None
    return {
        "filing_name": m.group("filing").lower(),
        "ticker": m.group("ticker").lower(),
        "issue_time": m.group("issue_date").replace("-", ""),
        "concept_id": m.group("concept"),
        "period": m.group("period").strip(),
    }


def _expected_output_path(
    fields: dict[str, str], model: str, output_root: Path
) -> Path:
    """Predict the file `write_audit.py` would write for these fields.

    Must stay in lock-step with `write_audit.py`'s filename construction —
    same `_sanitize` rule (`[^A-Za-z0-9._-]` → `-`), no lowercasing on model.
    """
    filename = (
        f"auditing_{fields['filing_name']}-{fields['ticker']}-{fields['issue_time']}_"
        f"{_sanitize(fields['concept_id'])}_{_sanitize(fields['period'])}_"
        f"{_sanitize(model)}.json"
    )
    return output_root / filename


def _load_prompt_file(
    path: Path, data_root: Path, output_root: Path
) -> list[tuple[str, str | None]]:
    """Read `path`, substitute `{env_dir}` / `{result_dir}`, extract case_id.

    Returns one (prompt, case_id) tuple per non-blank line. `case_id` is
    extracted from `(id: <token>)` if present, else None.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Tasks file does not exist: {path}")
    cases: list[tuple[str, str | None]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        prompt = line.replace("{env_dir}", str(data_root))
        prompt = prompt.replace("{result_dir}", str(output_root))
        m = _CASE_ID_RE.search(prompt)
        case_id = m.group(1).strip() if m else None
        cases.append((prompt, case_id))
    return cases


def _resolve_batch_paths(config: AuditingBatchConfig) -> tuple[Path, Path]:
    """Same defaults as single-case mode; benchmark_root only consulted as fallback."""
    data_root = config.data_root
    output_root = config.output_root
    if data_root is None or output_root is None:
        if config.benchmark_root is None:
            raise ValueError(
                "Provide either --benchmark-root, or both --data-root and "
                "--output-root explicitly."
            )
        if data_root is None:
            data_root = config.benchmark_root / "data" / "auditing"
        if output_root is None:
            output_root = config.benchmark_root / "results" / "auditing"
    return data_root.expanduser().resolve(), output_root.expanduser().resolve()


def _precheck_batch(
    config: AuditingBatchConfig, data_root: Path, output_root: Path
) -> None:
    """Same precheck as single-case mode, plus existence of the tasks file."""
    root = config.project_root

    skill = root / SKILL_DIR / "SKILL.md"
    if not skill.is_file():
        raise FileNotFoundError(f"Auditing SKILL.md not found at {skill}.")

    mcp_json = root / SKILL_MCP_JSON
    if not mcp_json.is_file():
        raise FileNotFoundError(f".mcp.json not found at {mcp_json}.")

    _check_mcp_server_importable(root)

    if config.benchmark_root is not None and not config.benchmark_root.is_dir():
        raise FileNotFoundError(
            f"benchmark_root does not exist or is not a directory: "
            f"{config.benchmark_root}"
        )
    if not data_root.is_dir():
        raise FileNotFoundError(
            f"Auditing data root does not exist: {data_root}. "
            f"Pass --data-root or populate <benchmark_root>/data/auditing."
        )
    if not config.tasks_file.is_file():
        raise FileNotFoundError(f"Tasks file does not exist: {config.tasks_file}")

    output_root.mkdir(parents=True, exist_ok=True)
    probe = output_root / ".writable_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise PermissionError(
            f"Output directory is not writable: {output_root} ({exc})"
        ) from exc


def run_auditing_batch(
    config: AuditingBatchConfig,
    *,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_task_start: Callable[[str], None] | None = None,
    on_task_complete: Callable[[AuditingTaskResult], None] | None = None,
) -> AuditingBatchResult:
    """Run all auditing prompts in `config.tasks_file` serially.

    Each prompt is run as one independent agent invocation (same lifecycle
    as `run_auditing`). The runner appends the model / output-root pinning
    clauses automatically, so the input file only needs to contain the audit
    body itself plus `{env_dir}` / `{result_dir}` placeholders.
    """
    data_root, output_root = _resolve_batch_paths(config)
    _precheck_batch(config, data_root, output_root)

    mcp_servers = _load_mcp_servers(config.project_root, data_root)
    resolved_model = config.model or resolve_model()

    cases = _load_prompt_file(config.tasks_file, data_root, output_root)
    if not cases:
        raise ValueError(
            f"No prompts found in {config.tasks_file} "
            f"(file is empty or all lines are blank)."
        )

    # Run log — one file per batch invocation, written to output_root.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = output_root / f"run_auditing_{ts}.log"
    log_file = log_path.open("w", buffering=1, encoding="utf-8")
    log_lock = threading.Lock()
    log_file.write(
        f"# auditing batch  tasks_file={config.tasks_file}  "
        f"model={resolved_model}  workers={config.workers}\n"
        f"# started {datetime.now(timezone.utc).isoformat()}\n\n"
    )

    stop_event = threading.Event()

    def _run_one(idx: int, raw_prompt: str, case_id: str | None) -> AuditingTaskResult:
        label = case_id or f"task_{idx}"
        full_prompt = raw_prompt + _audit_run_clauses(resolved_model, output_root)

        # Resume guard — predict the output filename deterministically.
        if config.resume:
            fields = _parse_audit_prompt(raw_prompt)
            if fields is not None:
                expected = _expected_output_path(fields, resolved_model, output_root)
                if expected.is_file():
                    skip = AuditingTaskResult(
                        case_id=case_id,
                        prompt=full_prompt,
                        agent_result=None,
                        output_path=expected,
                        skipped=True,
                    )
                    with log_lock:
                        log_file.write(f"[task] {label}  SKIP (resume)  → {expected}\n")
                        log_file.flush()
                    if on_task_complete:
                        on_task_complete(skip)
                    return skip

        if stop_event.is_set():
            # fail-fast triggered by another worker — return a placeholder error.
            return AuditingTaskResult(
                case_id=case_id,
                prompt=full_prompt,
                agent_result=AgentResult(
                    result="aborted (fail-fast)",
                    cost_usd=0.0,
                    turns=0,
                    duration_ms=0,
                    session_id="",
                    is_error=True,
                ),
                output_path=None,
            )

        if on_task_start:
            on_task_start(label)

        agent_result = run_agent(
            prompt=full_prompt,
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

        # Output path: use the deterministic predicted path; fall back to None.
        output_path: Path | None = None
        fields = _parse_audit_prompt(raw_prompt)
        if fields is not None:
            predicted = _expected_output_path(fields, resolved_model, output_root)
            if predicted.is_file():
                output_path = predicted

        task = AuditingTaskResult(
            case_id=case_id,
            prompt=full_prompt,
            agent_result=agent_result,
            output_path=output_path,
        )

        with log_lock:
            log_file.write(
                f"[task] {label}  "
                f"{'ERROR' if agent_result.is_error else 'OK'}  "
                f"cost=${agent_result.cost_usd:.4f}  turns={agent_result.turns}  "
                f"→ {output_path or '(no output file)'}\n"
            )
            log_file.flush()

        if on_task_complete:
            on_task_complete(task)

        if config.fail_fast and agent_result.is_error:
            logger.warning("fail-fast: stopping after error on %s", label)
            stop_event.set()

        return task

    # Submit all tasks; results are collected in input order.
    per_task: list[AuditingTaskResult] = [None] * len(cases)  # type: ignore[list-item]
    total_cost = 0.0
    num_errors = 0
    num_skipped = 0

    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        future_to_idx = {
            executor.submit(_run_one, idx, raw_prompt, case_id): idx - 1
            for idx, (raw_prompt, case_id) in enumerate(cases, start=1)
        }
        with tqdm(
            total=len(cases),
            desc="auditing",
            unit="task",
            dynamic_ncols=True,
        ) as pbar:
            for future in as_completed(future_to_idx):
                slot = future_to_idx[future]
                result = future.result()
                per_task[slot] = result
                if result.skipped:
                    num_skipped += 1
                    pbar.set_postfix(ok=len(cases) - num_errors - num_skipped, err=num_errors, skip=num_skipped)
                elif result.agent_result and result.agent_result.is_error:
                    num_errors += 1
                    total_cost += result.agent_result.cost_usd
                    pbar.set_postfix(ok=len(cases) - num_errors - num_skipped, err=num_errors, skip=num_skipped)
                elif result.agent_result:
                    total_cost += result.agent_result.cost_usd
                    pbar.set_postfix(ok=len(cases) - num_errors - num_skipped, err=num_errors, skip=num_skipped)
                pbar.update(1)

    with log_lock:
        log_file.write(
            f"\n# finished {datetime.now(timezone.utc).isoformat()}  "
            f"total_cost=${total_cost:.4f}  errors={num_errors}  skipped={num_skipped}\n"
        )
        log_file.close()
    logger.info("run log written to %s", log_path)

    return AuditingBatchResult(
        config=config,
        per_task=per_task,
        total_cost_usd=total_cost,
        num_errors=num_errors,
        num_skipped=num_skipped,
    )


def _find_output_file(output_dir: Path, config: AuditingConfig) -> Path | None:
    """Return the audit JSON `write_audit.py` produced for this config.

    `write_audit.py` writes:
        auditing_{filing_name}-{ticker}-{issue_time}_<sanitized concept>_<sanitized period>_<sanitized model>.json

    We glob on the unsanitized prefix (filing-ticker-issue_time) and prefer
    candidates that also contain the sanitized concept token, then pick the
    freshest.
    """
    if not output_dir.is_dir():
        return None
    prefix = f"auditing_{config.filing_name}-{config.ticker}-{config.issue_time}_"
    candidates = list(output_dir.glob(prefix + "*.json"))
    if not candidates:
        return None
    concept_token = _sanitize(config.concept_id)
    narrow = [p for p in candidates if concept_token in p.name]
    if narrow:
        candidates = narrow
    return max(candidates, key=lambda p: p.stat().st_mtime)
