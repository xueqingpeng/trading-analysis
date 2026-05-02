"""CLI entry point for financial_agentic_benchmark automation."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .benchmark import (
    BatchRunResult,
    BenchmarkTask,
    BenchmarkRunResult,
    DEFAULT_PROJECT_ROOT,
    load_tasks_file,
    run_benchmark_batch,
    run_benchmark_task,
)
from .trading_daily import (
    TradingDailyConfig,
    TradingRangeResult,
    run_trading_range,
)
from .hedging_daily import (
    HedgingDailyConfig,
    HedgingRangeResult,
    run_hedging_range,
)
from .auditing_runner import (
    AuditingBatchConfig,
    AuditingBatchResult,
    AuditingConfig,
    AuditingResult,
    AuditingTaskResult,
    run_auditing,
    run_auditing_batch,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-agent-trading",
        description="Run financial_agentic_benchmark tasks through Claude Agent SDK",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch_parser = subparsers.add_parser("batch", help="Run a JSONL batch of tasks")
    _add_common_runtime_args(batch_parser)
    batch_parser.add_argument("--tasks-file", required=True, help="Path to JSONL task file")
    batch_parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failed task")

    trading_parser = subparsers.add_parser(
        "trading",
        help="Drive the single-day trading skill once per day over a date range",
    )
    _add_trading_daily_args(trading_parser)

    hedging_parser = subparsers.add_parser(
        "hedging",
        help="Drive the single-day hedging skill once per day over a date range "
             "(first day is IS_FIRST_DAY=True; pair selection happens then)",
    )
    _add_hedging_daily_args(hedging_parser)

    report_gen_parser = subparsers.add_parser(
        "report-generation",
        help="Run the report_generation skill",
    )
    _add_single_task_common_args(report_gen_parser)
    report_gen_parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. TSLA")
    report_gen_parser.add_argument("--data-root", default=None, help="Trading parquet directory")
    report_gen_parser.add_argument("--output-root", default=None, help="Output directory for markdown reports")

    report_eval_parser = subparsers.add_parser(
        "report-evaluation",
        help="Run the report_evaluation skill",
    )
    _add_single_task_common_args(report_eval_parser)
    report_eval_parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. TSLA")
    report_eval_parser.add_argument("--target-agent", required=True, help="Agent name embedded in report filenames")
    report_eval_parser.add_argument("--target-model", required=True, help="Model name embedded in report filenames")
    report_eval_parser.add_argument("--data-root", default=None, help="Trading parquet directory")
    report_eval_parser.add_argument("--reports-root", default=None, help="Parent directory of report_generation outputs")
    report_eval_parser.add_argument("--output-root", default=None, help="Output directory for evaluation JSON")

    auditing_parser = subparsers.add_parser(
        "auditing",
        help="Run the auditing skill — one case via per-case flags, or many "
             "cases via --tasks-file batch mode",
    )
    _add_auditing_args(auditing_parser)

    args = parser.parse_args()
    callbacks = _build_callbacks(args.verbose)

    if args.command == "batch":
        tasks = load_tasks_file(args.tasks_file)
        tasks = [_apply_batch_defaults(task, args) for task in tasks]
        result = run_benchmark_batch(
            tasks,
            tasks_file=args.tasks_file,
            fail_fast=args.fail_fast,
            **callbacks,
        )
        _emit_result(result, as_json=args.json)
        if any(r.agent_result.is_error for r in result.results):
            sys.exit(1)
        return

    if args.command == "trading":
        trading_result = _run_trading_from_args(args, callbacks)
        _emit_trading_range_result(trading_result, as_json=args.json)
        if trading_result.config.fail_fast and trading_result.num_errors > 0:
            sys.exit(1)
        return

    if args.command == "hedging":
        hedging_result = _run_hedging_from_args(args, callbacks)
        _emit_hedging_range_result(hedging_result, as_json=args.json)
        if hedging_result.config.fail_fast and hedging_result.num_errors > 0:
            sys.exit(1)
        return

    if args.command == "auditing":
        if args.tasks_file:
            batch_result = _run_auditing_batch_from_args(args, callbacks)
            _emit_auditing_batch_result(batch_result, as_json=args.json)
            if batch_result.config.fail_fast and batch_result.num_errors > 0:
                sys.exit(1)
            return
        auditing_result = _run_auditing_from_args(args, callbacks)
        _emit_auditing_result(auditing_result, as_json=args.json)
        if auditing_result.agent_result.is_error:
            sys.exit(1)
        return

    task = _task_from_args(args)
    result = run_benchmark_task(task, **callbacks)
    _emit_result(result, as_json=args.json)
    if result.agent_result.is_error:
        sys.exit(1)


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--benchmark-root",
        required=True,
        help="financial_agentic_benchmark root directory",
    )
    parser.add_argument("--model", default=None, help="Claude model override")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agent turns")
    parser.add_argument("--max-budget", type=float, default=None, help="Cost cap in USD")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print assistant and tool events to stderr")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")


def _add_single_task_common_args(parser: argparse.ArgumentParser) -> None:
    _add_common_runtime_args(parser)


def _build_callbacks(verbose: bool) -> dict[str, object]:
    callbacks: dict[str, object] = {}
    if verbose:
        callbacks["on_assistant_text"] = lambda text: print(f"[assistant] {text[:200]}", file=sys.stderr)
        callbacks["on_thinking"] = lambda text: print(f"[thinking] {text[:500]}", file=sys.stderr)
        callbacks["on_tool_use"] = lambda name, inp: print(f"[tool] {name} {inp}", file=sys.stderr)
        callbacks["on_stderr"] = lambda line: print(f"[stderr] {line}", file=sys.stderr)
    return callbacks


def _task_from_args(args: argparse.Namespace) -> BenchmarkTask:
    task_type = args.command.replace("-", "_")
    payload = {
        "task_type": task_type,
        "benchmark_root": args.benchmark_root,
        "model": args.model,
        "max_turns": args.max_turns,
        "max_budget_usd": args.max_budget,
    }

    if task_type == "report_generation":
        payload.update(
            {
                "ticker": args.ticker,
                "data_root": args.data_root,
                "output_root": args.output_root,
            }
        )
    elif task_type == "report_evaluation":
        payload.update(
            {
                "ticker": args.ticker,
                "target_agent": args.target_agent,
                "target_model": args.target_model,
                "data_root": args.data_root,
                "reports_root": args.reports_root,
                "output_root": args.output_root,
            }
        )
    else:
        raise ValueError(
            f"_task_from_args does not handle task_type={task_type!r}. "
            "trading / hedging / auditing each have dedicated runners; "
            "this helper is only for report-generation / report-evaluation."
        )
    return BenchmarkTask(**payload)


def _apply_batch_defaults(task: BenchmarkTask, args: argparse.Namespace) -> BenchmarkTask:
    payload = asdict(task)
    payload["benchmark_root"] = task.benchmark_root or args.benchmark_root
    payload["model"] = task.model or args.model
    payload["max_turns"] = task.max_turns if task.max_turns is not None else args.max_turns
    payload["max_budget_usd"] = (
        task.max_budget_usd if task.max_budget_usd is not None else args.max_budget
    )
    return BenchmarkTask(**payload)


def _emit_result(result: BenchmarkRunResult | BatchRunResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    if isinstance(result, BatchRunResult):
        num_errors = sum(1 for r in result.results if r.agent_result.is_error)
        print(f"Batch completed: {len(result.results)} task(s), {num_errors} error(s).")
        for run in result.results:
            status = "ERROR" if run.agent_result.is_error else "OK"
            print(
                f"  {status} {run.task.task_type} "
                f"cost=${run.agent_result.cost_usd:.4f} "
                f"turns={run.agent_result.turns}"
            )
        return

    ar = result.agent_result
    status = "ERROR" if ar.is_error else "OK"
    print(f"{status} {result.task.task_type}")
    if ar.result:
        print(ar.result)
    print(
        f"\n--- Cost: ${ar.cost_usd:.4f} | Turns: {ar.turns} | Duration: {ar.duration_ms}ms ---",
        file=sys.stderr,
    )


# ------------------------ trading (daily-loop) ------------------------

def _add_trading_daily_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", required=True, help="Stock symbol, e.g. TSLA")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--output",
        required=True,
        help="Directory the skill writes the result JSON into each day "
             "(passed to upsert_decision.py via --output-root)",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the DuckDB file the MCP server reads from",
    )
    parser.add_argument("--model", default=None, help="Claude model override")
    parser.add_argument("--max-turns", type=int, default=30, help="Agent max turns per day")
    parser.add_argument(
        "--max-budget",
        type=float,
        default=1.0,
        help="Per-day cost cap in USD (default 1.0)",
    )
    parser.add_argument(
        "--skip-weekends",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip Sat/Sun before invoking the agent (default on; market holidays handled by the skill)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first day that returns an error",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print assistant and tool events to stderr",
    )
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")


def _run_trading_from_args(
    args: argparse.Namespace, callbacks: dict[str, object]
) -> TradingRangeResult:
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        raise SystemExit(f"Invalid --start/--end (expected YYYY-MM-DD): {exc}")

    if end < start:
        raise SystemExit(f"--end ({end}) must be >= --start ({start})")

    config = TradingDailyConfig(
        symbol=args.symbol,
        start=start,
        end=end,
        output_dir=Path(args.output).expanduser().resolve(),
        db_path=Path(args.db_path).expanduser().resolve(),
        project_root=DEFAULT_PROJECT_ROOT.resolve(),
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
        skip_weekends=args.skip_weekends,
        fail_fast=args.fail_fast,
    )

    day_callbacks = {
        "on_day_start": lambda d: print(f"[day] {d} → invoking agent", file=sys.stderr),
        "on_day_complete": lambda r: print(
            f"[day] {r.date} {'ERROR' if r.agent_result.is_error else 'OK'} "
            f"cost=${r.agent_result.cost_usd:.4f} turns={r.agent_result.turns} "
            f"→ {r.output_path or '(no output file)'}",
            file=sys.stderr,
        ),
    }
    return run_trading_range(config, **callbacks, **day_callbacks)


def _emit_trading_range_result(
    result: TradingRangeResult, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"Trading range: {result.config.symbol} "
          f"{result.config.start} → {result.config.end}")
    for d in result.per_day:
        status = "ERROR" if d.agent_result.is_error else "OK"
        dest = str(d.output_path) if d.output_path else "(no output)"
        print(
            f"  {d.date}  {status:<5}  ${d.agent_result.cost_usd:.4f}  "
            f"turns={d.agent_result.turns}  → {dest}"
        )
    print(
        f"\nTotal: {len(result.per_day)} day(s), "
        f"{result.num_errors} error(s), "
        f"${result.total_cost_usd:.4f}"
    )


# ------------------------ hedging (daily-loop) ------------------------

def _add_hedging_daily_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--start",
        required=True,
        help="Start date YYYY-MM-DD (the run's IS_FIRST_DAY=True day; "
             "pair selection happens here)",
    )
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--output",
        required=True,
        help="Directory the skill writes the result JSON into each day "
             "(passed to upsert_hedging_decision.py via --output-root). "
             "Must NOT contain a prior hedging_*.json file at start.",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the DuckDB file the MCP server reads from",
    )
    parser.add_argument("--model", default=None, help="Claude model override")
    parser.add_argument("--max-turns", type=int, default=30, help="Agent max turns per day")
    parser.add_argument(
        "--max-budget",
        type=float,
        default=1.0,
        help="Per-day cost cap in USD (default 1.0)",
    )
    parser.add_argument(
        "--skip-weekends",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip Sat/Sun before invoking the agent (default on; market holidays handled by the skill)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first day that returns an error",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print assistant and tool events to stderr",
    )
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")


def _run_hedging_from_args(
    args: argparse.Namespace, callbacks: dict[str, object]
) -> HedgingRangeResult:
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        raise SystemExit(f"Invalid --start/--end (expected YYYY-MM-DD): {exc}")

    if end < start:
        raise SystemExit(f"--end ({end}) must be >= --start ({start})")

    config = HedgingDailyConfig(
        start=start,
        end=end,
        output_dir=Path(args.output).expanduser().resolve(),
        db_path=Path(args.db_path).expanduser().resolve(),
        project_root=DEFAULT_PROJECT_ROOT.resolve(),
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
        skip_weekends=args.skip_weekends,
        fail_fast=args.fail_fast,
    )

    day_callbacks = {
        "on_day_start": lambda d: print(f"[day] {d} → invoking agent", file=sys.stderr),
        "on_day_complete": lambda r: print(
            f"[day] {r.date} {'ERROR' if r.agent_result.is_error else 'OK'} "
            f"cost=${r.agent_result.cost_usd:.4f} turns={r.agent_result.turns} "
            f"→ {r.output_path or '(no output file)'}",
            file=sys.stderr,
        ),
    }
    return run_hedging_range(config, **callbacks, **day_callbacks)


def _emit_hedging_range_result(
    result: HedgingRangeResult, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"Hedging range: {result.config.start} → {result.config.end}")
    for d in result.per_day:
        status = "ERROR" if d.agent_result.is_error else "OK"
        dest = str(d.output_path) if d.output_path else "(no output)"
        print(
            f"  {d.date}  {status:<5}  ${d.agent_result.cost_usd:.4f}  "
            f"turns={d.agent_result.turns}  → {dest}"
        )
    print(
        f"\nTotal: {len(result.per_day)} day(s), "
        f"{result.num_errors} error(s), "
        f"${result.total_cost_usd:.4f}"
    )


# ------------------------ auditing (single-shot) ------------------------

def _add_auditing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--benchmark-root", default=None,
        help="Optional. financial_agentic_benchmark root directory used to "
             "derive default --data-root (= <root>/data/auditing) and "
             "--output-root (= <root>/results/auditing). Not needed when both "
             "--data-root and --output-root are passed explicitly.",
    )
    # Mode: single case (six --filing-name/--ticker/... flags) OR batch via --tasks-file.
    # --tasks-file takes precedence; the per-case flags are only required in
    # single-case mode. argparse's `required` is checked at validation time
    # in _run_auditing_from_args (so --tasks-file can omit them).
    parser.add_argument(
        "--tasks-file", default=None,
        help="Path to a text file with one auditing prompt per line. Each line "
             "may contain {env_dir} / {result_dir} placeholders, which are "
             "substituted with --data-root / --output-root before sending. "
             "When set, the agent runs each prompt as an independent audit "
             "in a single docker invocation. Mutually exclusive with the "
             "per-case flags below.",
    )
    parser.add_argument(
        "--filing-name", default=None, choices=["10k", "10q"],
        help="(single-case only) Filing type, lowercase",
    )
    parser.add_argument(
        "--ticker", default=None,
        help="(single-case only) Lowercase company ticker as it appears in filing folder names",
    )
    parser.add_argument(
        "--issue-time", default=None,
        help="(single-case only) Filing issue date in YYYYMMDD format",
    )
    parser.add_argument(
        "--concept-id", default=None,
        help="(single-case only) Concept identifier including namespace, e.g. us-gaap:AssetsCurrent",
    )
    parser.add_argument(
        "--period", default=None,
        help="(single-case only) Requested period string (e.g. 'FY2023', "
             "'Q3 2023', '2023-12-31', '2023-01-01 to 2023-12-31')",
    )
    parser.add_argument(
        "--case-id", default=None,
        help="(single-case only) Task identifier embedded in the prompt (for benchmark traceability)",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="Auditing data directory (default: <benchmark-root>/data/auditing). "
             "Injected into auditing_mcp via --data-root.",
    )
    parser.add_argument(
        "--output-root", default=None,
        help="Output directory for the audit JSON (default: "
             "<benchmark-root>/results/auditing). Passed to write_audit.py "
             "via --output-root.",
    )
    parser.add_argument("--model", default=None, help="Claude model override")
    parser.add_argument("--max-turns", type=int, default=30, help="Agent max turns")
    parser.add_argument(
        "--max-budget", type=float, default=5.0,
        help="Cost cap in USD per task (default 5.0; auditing tasks are typically heavier than trading days)",
    )
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="(batch mode only) Stop after the first task that returns an error",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="(batch mode only) Re-run every task even if its predicted output "
             "file already exists. Default behaviour is to skip cases whose "
             "output file is already present (resume an interrupted batch).",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="(batch mode only) Number of parallel worker threads (default 1). "
             "Tasks are independent so 10-15 workers is safe with a paid API.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print assistant and tool events to stderr",
    )
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")


def _resolve_optional_benchmark_root(
    raw: str | None, *, data_root: str | None, output_root: str | None
) -> Path | None:
    """Validate --benchmark-root if given; require it only when defaults are needed."""
    if raw:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(
                f"--benchmark-root does not exist or is not a directory: {path}"
            )
        return path
    if not data_root or not output_root:
        raise SystemExit(
            "Provide either --benchmark-root, or both --data-root and "
            "--output-root explicitly."
        )
    return None


def _run_auditing_from_args(
    args: argparse.Namespace, callbacks: dict[str, object]
) -> AuditingResult:
    benchmark_root = _resolve_optional_benchmark_root(
        args.benchmark_root, data_root=args.data_root, output_root=args.output_root,
    )

    # Single-case mode requires the six per-case flags (argparse can't enforce
    # this directly because --tasks-file is the alternative).
    missing = [
        flag for flag, val in [
            ("--filing-name", args.filing_name),
            ("--ticker", args.ticker),
            ("--issue-time", args.issue_time),
            ("--concept-id", args.concept_id),
            ("--period", args.period),
            ("--case-id", args.case_id),
        ] if not val
    ]
    if missing:
        raise SystemExit(
            "Single-case auditing requires: "
            f"{', '.join(missing)}. Or pass --tasks-file for batch mode."
        )

    config = AuditingConfig(
        filing_name=args.filing_name.lower(),
        ticker=args.ticker.lower(),
        issue_time=args.issue_time,
        concept_id=args.concept_id,
        period=args.period,
        case_id=args.case_id,
        benchmark_root=benchmark_root,
        data_root=Path(args.data_root).expanduser().resolve() if args.data_root else None,
        output_root=Path(args.output_root).expanduser().resolve() if args.output_root else None,
        project_root=DEFAULT_PROJECT_ROOT.resolve(),
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
    )
    return run_auditing(config, **callbacks)


def _run_auditing_batch_from_args(
    args: argparse.Namespace, callbacks: dict[str, object]
) -> AuditingBatchResult:
    benchmark_root = _resolve_optional_benchmark_root(
        args.benchmark_root, data_root=args.data_root, output_root=args.output_root,
    )

    tasks_file = Path(args.tasks_file).expanduser().resolve()
    if not tasks_file.is_file():
        raise SystemExit(f"--tasks-file does not exist: {tasks_file}")

    config = AuditingBatchConfig(
        tasks_file=tasks_file,
        benchmark_root=benchmark_root,
        data_root=Path(args.data_root).expanduser().resolve() if args.data_root else None,
        output_root=Path(args.output_root).expanduser().resolve() if args.output_root else None,
        project_root=DEFAULT_PROJECT_ROOT.resolve(),
        model=args.model,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget,
        fail_fast=args.fail_fast,
        resume=not args.no_resume,
        workers=args.workers,
    )

    def _format_complete(r: AuditingTaskResult) -> str:
        if r.skipped:
            return (
                f"[task] {r.case_id or '?'} SKIP (resume) "
                f"→ {r.output_path}"
            )
        return (
            f"[task] {r.case_id or '?'} "
            f"{'ERROR' if r.agent_result.is_error else 'OK'} "
            f"cost=${r.agent_result.cost_usd:.4f} "
            f"turns={r.agent_result.turns} "
            f"→ {r.output_path or '(no output file)'}"
        )

    task_callbacks = {
        "on_task_start": lambda label: print(
            f"[task] {label} → invoking agent", file=sys.stderr
        ),
        "on_task_complete": lambda r: print(_format_complete(r), file=sys.stderr),
    }
    return run_auditing_batch(config, **callbacks, **task_callbacks)


def _emit_auditing_batch_result(
    result: AuditingBatchResult, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"Auditing batch: {result.config.tasks_file}")
    for t in result.per_task:
        dest = str(t.output_path) if t.output_path else "(no output)"
        if t.skipped:
            print(f"  {t.case_id or '?':<12}  SKIP    (already present)         → {dest}")
            continue
        status = "ERROR" if t.agent_result.is_error else "OK"
        print(
            f"  {t.case_id or '?':<12}  {status:<5}  "
            f"${t.agent_result.cost_usd:.4f}  "
            f"turns={t.agent_result.turns}  → {dest}"
        )
    print(
        f"\nTotal: {len(result.per_task)} task(s), "
        f"{result.num_skipped} skipped, "
        f"{result.num_errors} error(s), "
        f"${result.total_cost_usd:.4f}"
    )


def _emit_auditing_result(result: AuditingResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    ar = result.agent_result
    status = "ERROR" if ar.is_error else "OK"
    cfg = result.config
    print(
        f"{status} auditing  "
        f"{cfg.filing_name}/{cfg.ticker}/{cfg.issue_time}  "
        f"{cfg.concept_id}  {cfg.period}"
    )
    if ar.result:
        print(ar.result)
    dest = str(result.output_path) if result.output_path else "(no output file)"
    print(f"  → {dest}")
    print(
        f"\n--- Cost: ${ar.cost_usd:.4f} | Turns: {ar.turns} | "
        f"Duration: {ar.duration_ms}ms ---",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
