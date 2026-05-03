import asyncio
from datetime import date

import exchange_calendars as xcals
import logfire
import typer
from dotenv import load_dotenv

load_dotenv(".env")

logfire.configure(service_name="financial-agents")
logfire.instrument_anthropic()
logfire.instrument_mcp()

app = typer.Typer()

_NYSE = xcals.get_calendar("XNYS")


@app.callback()
def call_back():
    pass


def _trading_dates(start: date, end: date) -> list[str]:
    sessions = _NYSE.sessions_in_range(start.isoformat(), end.isoformat())
    return [s.strftime("%Y-%m-%d") for s in sessions]


def _weekly_endings(start: date, end: date) -> list[str]:
    """Last NYSE session per ISO week (handles holiday-shortened weeks)."""
    last_per_week = {}
    for s in _NYSE.sessions_in_range(start.isoformat(), end.isoformat()):
        last_per_week[s.isocalendar()[:2]] = s
    return [s.strftime("%Y-%m-%d") for s in sorted(last_per_week.values())]


@app.command("trading")
def cmd_trading(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Stock symbol, e.g. AAPL"),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD (inclusive)"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD (inclusive)"),
    model: str = typer.Option(None, "--model", "-m", help="Model spec, e.g. anthropic:claude-sonnet-4-6, openai:gpt-4o, openrouter:vendor/model"),
    concurrency: int = typer.Option(1, "--concurrency", "-c", help="Max concurrent agent calls"),
    test: bool = typer.Option(False, "--test", help="Test mode: only run the first 3 trading days"),
    output_root: str = typer.Option(None, "--output-root", help="Output dir (default: results/trading)"),
):
    from src.trading_pipeline import run_pipeline

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d < start_d:
        raise typer.BadParameter("--end must be >= --start")

    dates = _trading_dates(start_d, end_d)
    if test:
        dates = dates[:3]
    inputs = [{"symbol": symbol, "date": d} for d in dates]
    asyncio.run(run_pipeline(inputs, concurrency=concurrency, model=model, output_root=output_root))


@app.command("report-generation")
def cmd_report_generation(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Stock symbol, e.g. AAPL"),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD (inclusive)"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD (inclusive)"),
    model: str = typer.Option(None, "--model", "-m"),
    concurrency: int = typer.Option(1, "--concurrency", "-c"),
    output_root: str = typer.Option(None, "--output-root", help="Output dir (default: results/report_generation)"),
):
    from src.report_generation_pipeline import run_pipeline

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d < start_d:
        raise typer.BadParameter("--end must be >= --start")

    weeks = _weekly_endings(start_d, end_d)
    inputs = [{"symbol": symbol, "date": d} for d in weeks]
    asyncio.run(run_pipeline(inputs, concurrency=concurrency, model=model, output_root=output_root))


@app.command("report-evaluation")
def cmd_report_evaluation(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Ticker whose reports we evaluate"),
    target_model: str = typer.Option(..., "--target-model", help="Model id whose generated reports we evaluate (e.g. claude-sonnet-4-6)"),
    model: str = typer.Option(None, "--model", "-m", help="Model running the evaluation"),
    reports_root: str = typer.Option(None, "--reports-root", help="Root of report_generation outputs"),
    output_root: str = typer.Option(None, "--output-root", help="Output dir (default: results/report_evaluation)"),
):
    from src.report_evaluation_pipeline import run_pipeline

    inputs = [{"symbol": symbol, "target_model": target_model}]
    asyncio.run(run_pipeline(inputs, model=model, reports_root=reports_root, output_root=output_root))


@app.command("hedging")
def cmd_hedging(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD (inclusive)"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD (inclusive)"),
    model: str = typer.Option(None, "--model", "-m"),
    output_root: str = typer.Option(None, "--output-root", help="Output dir (default: results/hedging)"),
):
    from src.hedging_pipeline import run_pipeline

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d < start_d:
        raise typer.BadParameter("--end must be >= --start")

    dates = _trading_dates(start_d, end_d)
    inputs = [{"date": d, "is_first_day": (i == 0)} for i, d in enumerate(dates)]
    asyncio.run(run_pipeline(inputs, model=model, output_root=output_root))


@app.command("auditing")
def cmd_auditing(
    ticker: str = typer.Option(..., "--ticker", help="Lowercase ticker, e.g. rrr"),
    filing_name: str = typer.Option(..., "--filing-name", help="10k or 10q"),
    issue_time: str = typer.Option(..., "--issue-time", help="YYYYMMDD"),
    concept_id: str = typer.Option(..., "--concept-id", help="e.g. us-gaap:AssetsCurrent"),
    period: str = typer.Option(..., "--period", help="e.g. FY2021, Q3 2022, 2021-12-31"),
    model: str = typer.Option(None, "--model", "-m"),
    data_root: str = typer.Option(None, "--data-root", help="XBRL tree root"),
    output_root: str = typer.Option(None, "--output-root", help="Output dir (default: results/auditing)"),
):
    from src.auditing_pipeline import run_pipeline

    inputs = [
        {
            "ticker": ticker,
            "filing_name": filing_name,
            "issue_time": issue_time,
            "concept_id": concept_id,
            "period": period,
        }
    ]
    asyncio.run(run_pipeline(inputs, model=model, data_root=data_root, output_root=output_root))


if __name__ == "__main__":
    app()
