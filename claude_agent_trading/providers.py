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


def resolve_provider_env() -> dict[str, str]:
    """Build env dict for the agent subprocess.

    Includes API credentials and optional base URL / model overrides.
    Loads from .env file at project root (without overriding existing env vars).

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

    # --- Small/fast model override ---
    # Claude CLI internally uses haiku for title generation, compact, etc.
    # When using a proxy, haiku won't exist — override with ANTHROPIC_SMALL_FAST_MODEL.
    small_model = os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
    if small_model:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small_model

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
