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
from .report_generation_daily import (
    ReportGenerationDailyConfig,
    ReportGenerationRangeResult,
    run_report_generation_range,
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

    report_gen_parser = subparsers.add_parser(
        "report-generation",
        help="Drive the single-day report_generation skill once per day over a date range",
    )
    _add_report_generation_daily_args(report_gen_parser)

    report_eval_parser = subparsers.add_parser(
        "report-evaluation",
        help="Run the report_evaluation skill",
    )
    _add_single_task_common_args(report_eval_parser)
    report_eval_parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. TSLA")
    report_eval_parser.add_argument("--target-agent", required=True, help="Agent name embedded in report filenames")
    report_eval_parser.add_argument("--target-model", required=True, help="Model name embedded in report filenames")
    report_eval_parser.add_argument("--db-path", required=True, help="Path to the DuckDB file the MCP server reads from")
    report_eval_parser.add_argument("--reports-root", default=None, help="Parent directory of report_generation outputs")
    report_eval_parser.add_argument("--output-root", default=None, help="Output directory for evaluation JSON")

    auditing_parser = subparsers.add_parser("auditing", help="Run the auditing skill")
    _add_single_task_common_args(auditing_parser)
    auditing_parser.add_argument("--filing-name", required=True, choices=["10k", "10q"], help="Filing type")
    auditing_parser.add_argument("--ticker", required=True, help="Lowercase company ticker in filing folders")
    auditing_parser.add_argument("--issue-time", required=True, help="Issue date in YYYYMMDD format")
    auditing_parser.add_argument("--concept-id", required=True, help="Concept identifier, e.g. us-gaap:AssetsCurrent")
    auditing_parser.add_argument("--period", required=True, help="Requested period string")
    auditing_parser.add_argument("--case-id", required=True, help="Task identifier used in output filename")
    auditing_parser.add_argument("--data-root", default=None, help="Auditing data directory")
    auditing_parser.add_argument("--output-root", default=None, help="Output directory for auditing JSON")

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

    if args.command == "report-generation":
        report_result = _run_report_generation_from_args(args, callbacks)
        _emit_report_generation_range_result(report_result, as_json=args.json)
        if report_result.config.fail_fast and report_result.num_errors > 0:
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

    if task_type == "report_evaluation":
        payload.update(
            {
                "ticker": args.ticker,
                "target_agent": args.target_agent,
                "target_model": args.target_model,
                "db_path": args.db_path,
                "reports_root": args.reports_root,
                "output_root": args.output_root,
            }
        )
    else:
        payload.update(
            {
                "filing_name": args.filing_name,
                "ticker": args.ticker,
                "issue_time": args.issue_time,
                "concept_id": args.concept_id,
                "period": args.period,
                "case_id": args.case_id,
                "data_root": args.data_root,
                "output_root": args.output_root,
            }
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


def _add_report_generation_daily_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", required=True, help="Stock symbol, e.g. TSLA")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--output",
        required=True,
        help="Directory the skill writes reports into each day "
             "(passed to upsert_report.py via --output-root)",
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
        help="Skip Sat/Sun before invoking the agent (default on)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first day that returns an error",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print events to stderr")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON")


def _run_report_generation_from_args(
    args: argparse.Namespace, callbacks: dict[str, object]
) -> ReportGenerationRangeResult:
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        raise SystemExit(f"Invalid --start/--end (expected YYYY-MM-DD): {exc}")

    if end < start:
        raise SystemExit(f"--end ({end}) must be >= --start ({start})")

    config = ReportGenerationDailyConfig(
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
    return run_report_generation_range(config, **callbacks, **day_callbacks)


def _emit_report_generation_range_result(
    result: ReportGenerationRangeResult, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"Report generation: {result.config.symbol} "
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


if __name__ == "__main__":
    main()
