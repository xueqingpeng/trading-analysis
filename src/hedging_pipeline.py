import asyncio
import json
import re
from pathlib import Path
from typing import Any, Iterable

import logfire

from src._common import DEFAULT_DB, PROJECT_ROOT, build_agent, model_id

# Retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0

# Filename sanitization regex
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

SKILL_DIR = "/skills/hedging/"
MCP_SCRIPT = PROJECT_ROOT / "skills" / "hedging" / "scripts" / "mcp" / "hedging_mcp.py"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "results" / "hedging"


def _mcp_servers(db_path: Path) -> dict:
    return {
        "hedging_mcp": {
            "command": "python3",
            "args": [str(MCP_SCRIPT), f"--db-path={db_path}"],
            "transport": "stdio",
        }
    }


def _is_retryable_error(error: Exception) -> bool:
    """
    Determine if an error is worth retrying.

    Returns True for transient errors (timeouts, rate limits, 5xx errors).
    Returns False for permanent errors (4xx, invalid requests, context limits).
    """
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # Non-retryable: Client errors, logic errors
    non_retryable_patterns = [
        "400",
        "401",
        "403",
        "404",
        "invalid request",
        "context length",
        "context_length_exceeded",
        "invalid_request_error",
    ]

    # Check non-retryable first (higher priority)
    if any(pattern in error_str for pattern in non_retryable_patterns):
        return False

    # Retryable: API errors, timeouts, rate limits, server errors
    retryable_patterns = [
        "timeout",
        "timed out",
        "rate limit",
        "ratelimit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "connection",
        "temporarily unavailable",
        "service unavailable",
        "overloaded",
    ]

    if any(pattern in error_str for pattern in retryable_patterns):
        return True

    # Check error types
    retryable_types = ["timeout", "connectionerror", "httperror"]
    if any(t in error_type for t in retryable_types):
        return True

    # Default: retry most errors (conservative approach for new model issues)
    return True


def _sanitize(value: str) -> str:
    """Sanitize a string for use in filenames."""
    return _FILENAME_SAFE_RE.sub("_", value)


def _record_error(
    date_str: str,
    error_msg: str,
    model_slug: str,
    output_root: Path,
    pair_info: str = "unknown",
) -> None:
    """
    Record a failed date and its error message to an error log file.

    Creates/updates: {output_root}/errors/hedging_{model}_errors.json

    Structure:
    {
        "status": "failed",
        "model": "gpt_5_4",
        "pair": "AAPL_MSFT",  # if known
        "errors": [
            {
                "date": "2026-01-05",
                "error": "Rate limit exceeded",
                "timestamp": "2026-05-03T10:30:45.123456"
            }
        ]
    }
    """
    from datetime import datetime

    # Create errors directory
    error_dir = output_root / "errors"
    error_dir.mkdir(parents=True, exist_ok=True)

    # Error file path (hedging_{model}_errors.json)
    error_filename = f"hedging_{model_slug}_errors.json"
    error_path = error_dir / error_filename

    # Load existing errors or create new
    if error_path.exists():
        try:
            with open(error_path, "r") as f:
                doc = json.load(f)
            if not isinstance(doc, dict) or not isinstance(doc.get("errors"), list):
                doc = {"status": "failed", "errors": []}
        except json.JSONDecodeError:
            doc = {"status": "failed", "errors": []}
    else:
        doc = {"status": "failed", "errors": []}

    # Add metadata
    doc["model"] = model_slug
    if pair_info != "unknown":
        doc["pair"] = pair_info

    # Check if this date already has an error recorded
    existing_dates = {err.get("date") for err in doc["errors"] if isinstance(err, dict)}

    if date_str not in existing_dates:
        # Add new error
        doc["errors"].append({
            "date": date_str,
            "error": error_msg,
            "timestamp": datetime.now().isoformat(),
        })

        # Sort by date
        doc["errors"].sort(key=lambda e: e.get("date", ""))

        # Write back
        with open(error_path, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)

        logfire.info(f"Recorded error for hedging on {date_str} to {error_path}")


async def run_one(
    agent,
    item: dict[str, Any],
    *,
    model_slug: str,
    output_root: Path,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
) -> tuple[bool, str]:
    """
    Run one hedging decision with retry logic.

    Args:
        agent: The agent to invoke
        item: Dict with 'date' and optionally 'is_first_day' keys
        model_slug: Sanitized model name for file naming
        output_root: Output directory path
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Base delay in seconds for exponential backoff (default: 1.0)

    Returns:
        Tuple of (success: bool, error_message: str)
        - (True, "") if successful
        - (False, error_msg) if failed after all retries
    """
    verb = "start" if item.get("is_first_day") else "run"
    prompt = (
        f"{verb} hedging on {item['date']}. "
        f"Use --model {model_slug} and --output-root {output_root} "
        f"when calling upsert_hedging_decision.py."
    )
    thread_id = item["date"]

    for attempt in range(max_retries):
        try:
            with logfire.span(
                "agent.invoke attempt={attempt} prompt={prompt!r}",
                attempt=attempt + 1,
                prompt=prompt,
                thread_id=thread_id,
            ):
                await agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"configurable": {"thread_id": thread_id}},
                )

            # Success!
            if attempt > 0:
                logfire.info(
                    f"✓ Retry succeeded on attempt {attempt + 1} for hedging on {item['date']}"
                )
            return (True, "")

        except Exception as e:
            error_msg = str(e)
            is_retryable = _is_retryable_error(e)

            # If non-retryable or last attempt, fail permanently
            if not is_retryable or attempt == max_retries - 1:
                if not is_retryable:
                    logfire.error(
                        f"✗ Non-retryable error for hedging on {item['date']}: {error_msg}"
                    )
                else:
                    logfire.exception(
                        f"✗ Failed after {max_retries} attempts for hedging on {item['date']}"
                    )
                return (False, error_msg)

            # Exponential backoff: 1s, 2s, 4s, ...
            delay = base_delay * (2 ** attempt)
            logfire.warning(
                f"⚠ Attempt {attempt + 1}/{max_retries} failed for hedging on {item['date']}, "
                f"retrying in {delay}s: {error_msg}"
            )
            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    return (False, "Max retries exceeded")


async def run_pipeline(
    inputs: Iterable[dict[str, Any]],
    *,
    model: str | None = None,
    db_path: Path = DEFAULT_DB,
    output_root: Path | str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> None:
    """
    Run hedging pipeline with retry logic and error handling.

    Note: Hedging runs serially (not concurrent) because day N reads the pair
    file that day N-1 wrote.

    Args:
        inputs: Iterable of dicts with 'date' and optionally 'is_first_day' keys
        model: Model spec (e.g., 'openai:gpt-5.4')
        db_path: Path to DuckDB file
        output_root: Output directory for results
        max_retries: Max retries per date (default: 3)
    """
    items = list(inputs)
    out = Path(output_root) if output_root else DEFAULT_OUTPUT_ROOT
    out.mkdir(parents=True, exist_ok=True)
    slug = model_id(model)

    with logfire.span(
        "hedging.run n={n} model={m} out={o} retries={r}",
        n=len(items),
        m=model,
        o=str(out),
        r=max_retries,
    ):
        agent = await build_agent(
            skill_dir=SKILL_DIR,
            mcp_servers=_mcp_servers(db_path),
            model=model,
        )

        # Serial execution: day N reads the pair file day N-1 wrote
        successes = []
        failures = []

        for it in items:
            success, error = await run_one(
                agent, it, model_slug=slug, output_root=out, max_retries=max_retries
            )

            if success:
                successes.append(it)
            else:
                failures.append({**it, "error": error})
                # Record to error file
                _record_error(
                    date_str=it.get("date", "unknown"),
                    error_msg=error,
                    model_slug=slug,
                    output_root=out,
                )
                # For hedging, we might want to stop on failure since days depend on each other
                # But we'll continue for now to match trading behavior

        # Log summary
        total = len(items)
        success_count = len(successes)
        failure_count = len(failures)
        success_rate = (success_count / total * 100) if total > 0 else 0

        logfire.info(
            f"Hedging pipeline complete: {success_count}/{total} succeeded ({success_rate:.1f}%), "
            f"{failure_count} failed"
        )

        if failures:
            failed_dates = [f["date"] for f in failures]
            logfire.warning(f"Failed dates: {', '.join(failed_dates)}")
