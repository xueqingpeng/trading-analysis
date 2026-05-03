import os
from pathlib import Path

import logfire
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_mcp_adapters.client import MultiServerMCPClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "env.duckdb"

_KNOWN_PROVIDERS = ("openai", "anthropic", "openrouter")


def _normalize_spec(spec: str) -> str:
    """Strip an accidentally repeated provider prefix (e.g. "openai:openai:gpt-5"
    -> "openai:gpt-5"). Catches a common typo that otherwise fails late at
    inference time instead of early."""
    for p in _KNOWN_PROVIDERS:
        dup = f"{p}:{p}:"
        if spec.startswith(dup):
            return p + ":" + spec[len(dup):]
    return spec


def model_id(spec: str | None) -> str:
    """Bare model name suitable for filename suffix (no provider prefix).

    Sanitizes the model name to be filesystem-safe by replacing any character
    that is not alphanumeric, underscore, or hyphen with an underscore.
    This matches the sanitization logic in upsert_decision.py.

    Examples:
      None                            -> "claude-sonnet-4-6"  (deepagents default)
      "openai:gpt-4o"                 -> "gpt-4o"
      "openai:gpt-5.4"                -> "gpt-5_4"  (dot replaced)
      "anthropic:claude-sonnet-4-6"   -> "claude-sonnet-4-6"
      "claude-sonnet-4-6"             -> "claude-sonnet-4-6"
      "openrouter:vendor/model"       -> "vendor_model"
    """
    import re

    if spec is None:
        return "claude-sonnet-4-6"
    s = _normalize_spec(spec)
    for p in _KNOWN_PROVIDERS:
        prefix = f"{p}:"
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Replace slashes AND any non-alphanumeric/underscore/hyphen chars
    # to match upsert_decision.py's _sanitize() logic
    s = s.replace("/", "_")
    s = re.sub(r"[^A-Za-z0-9_-]", "_", s)
    return s


def _resolve_model(spec: str | None):
    """Resolve a model spec to either a string (passed through to deepagents
    -> init_chat_model) or a pre-built chat model instance.

    Supported forms:
      - None                          -> deepagents default (claude-sonnet-4-6)
      - "openai:gpt-4o"               -> raw OpenAI API (init_chat_model picks ChatOpenAI w/ OPENAI_API_KEY)
      - "anthropic:claude-sonnet-4-6" -> raw Anthropic API (init_chat_model picks ChatAnthropic w/ ANTHROPIC_API_KEY)
      - "claude-sonnet-4-6"           -> assumed anthropic, then as above
      - "openrouter:vendor/model"     -> ChatOpenAI pointed at OpenRouter base_url + OPENROUTER_API_KEY
    """
    if spec is None:
        return None
    spec = _normalize_spec(spec)

    # OpenRouter — only branch that needs custom wiring (different base_url + key).
    if spec.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=spec.removeprefix("openrouter:"),
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )

    # OpenAI — raw OpenAI API. Passing the "openai:..." string straight to
    # create_deep_agent makes deepagents call init_chat_model, which builds a
    # plain ChatOpenAI client (default base_url=https://api.openai.com,
    # OPENAI_API_KEY). No OpenRouter, no proxy.
    if spec.startswith("openai:"):
        return spec

    # Anthropic — bare claude-* assumed anthropic.
    if spec.startswith("claude-"):
        return f"anthropic:{spec}"

    # anything else (e.g. "anthropic:...") -> deepagents/init_chat_model passthrough
    return spec


async def build_agent(*, skill_dir: str, mcp_servers: dict, model: str | None):
    """Generic agent builder shared by every task pipeline."""
    client = MultiServerMCPClient(mcp_servers)
    with logfire.span("agent.load_mcp_tools skill={skill}", skill=skill_dir):
        tools = await client.get_tools()
        logfire.info(
            "loaded {n} MCP tools: {names}",
            n=len(tools),
            names=[t.name for t in tools],
        )
    with logfire.span("agent.build skill={skill} model={model}", skill=skill_dir, model=model):
        return create_deep_agent(
            model=_resolve_model(model),
            # LocalShellBackend (not FilesystemBackend) so the agent's `execute`
            # tool can actually run subprocesses — the trading SKILL writes its
            # output via `python3 .claude/skills/trading/scripts/upsert_decision.py`,
            # which only works with a backend implementing SandboxBackendProtocol.
            # inherit_env=True so subprocesses see PATH, ANTHROPIC_API_KEY,
            # OPENAI_API_KEY, etc. cwd is PROJECT_ROOT.
            backend=LocalShellBackend(
                root_dir=str(PROJECT_ROOT),
                virtual_mode=True,
                inherit_env=True,
            ),
            skills=[skill_dir],
            tools=tools,
        )
