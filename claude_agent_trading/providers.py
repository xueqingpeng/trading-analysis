"""Provider credential and configuration resolution."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (next to this package)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def resolve_model() -> str:
    """Resolve model from env. CLI --model takes priority (handled in caller)."""
    return os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


def resolve_provider_env(model: str | None = None) -> dict[str, str]:
    """Build env dict for the agent subprocess.

    Includes API credentials, optional base URL, and unified model aliases.
    Loads from .env file at project root (without overriding existing env vars).

    Args:
        model: The main model to use. If given, also pinned to haiku/sonnet/opus/
               subagent aliases so every internal Claude CLI call uses the same
               model. Needed for proxy mode where third-party APIs don't know
               the built-in claude-haiku-* / claude-opus-* names.

    Returns:
        Dict of env vars to pass to the agent subprocess.

    Raises:
        RuntimeError: If no valid credentials are found.
    """
    env: dict[str, str] = {}

    # --- API base URL (proxy / custom endpoint) ---
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url

    # --- Unify every model alias to the main model ---
    # Claude CLI internally issues calls with haiku (compact/title/background),
    # opus (plan mode), and a subagent model. Through a proxy to third-party
    # APIs those built-in names 404. Pin them all to the user-selected model.
    # ANTHROPIC_SMALL_FAST_MODEL was the old knob; it's deprecated in favor
    # of ANTHROPIC_DEFAULT_HAIKU_MODEL. We set all four for completeness.
    effective_model = model or os.environ.get("CLAUDE_MODEL") or "claude-sonnet-4-6"
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = effective_model
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = effective_model
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = effective_model
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = effective_model

    # --- Azure Foundry mode ---
    foundry_keys = [
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "CLAUDE_CODE_USE_FOUNDRY",
    ]
    foundry_vals = {k: os.environ.get(k) for k in foundry_keys}

    if all(foundry_vals.values()):
        for k, v in foundry_vals.items():
            if v:
                env[k] = v
        return env

    # --- Direct API mode ---
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        return env

    raise RuntimeError(
        "No Claude API credentials found. Set either:\n"
        "  Azure Foundry: ANTHROPIC_FOUNDRY_RESOURCE, ANTHROPIC_FOUNDRY_API_KEY, CLAUDE_CODE_USE_FOUNDRY=1\n"
        "  Direct API:    ANTHROPIC_API_KEY\n"
        "You can put these in .env at the project root."
    )
