"""Core module: run_agent() and AgentResult."""

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, ResultMessage, SystemMessage

from .providers import resolve_model, resolve_provider_env

logger = logging.getLogger("claude_agent_framework")


@dataclass
class AgentResult:
    """Result returned by run_agent()."""

    result: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    duration_ms: int = 0
    session_id: str = ""
    is_error: bool = False
    thinking: list[str] = field(default_factory=list)


def run_agent(
    prompt: str,
    *,
    cwd: str | Path | None = None,
    model: str | None = None,
    max_turns: int = 30,
    max_budget_usd: float = 5.0,
    max_thinking_tokens: int | None = None,
    env: dict[str, str] | None = None,
    permission_mode: str = "bypassPermissions",
    setting_sources: list[str] | None = None,
    mcp_servers: dict | str | Path | None = None,
    disallowed_tools: list[str] | None = None,
    allow_web: bool = False,
    on_assistant_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_use: Callable[[str, dict], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run a Claude agent session and return the result.

    Skills are discovered automatically by the Agent SDK from .claude/skills/
    under cwd when setting_sources includes "project". No explicit skill
    registration is needed — the agent matches skills based on prompt content.

    Args:
        prompt: The task prompt to send to the agent.
        cwd: Working directory for the agent. Should contain .claude/skills/
             for skill auto-discovery.
        model: Claude model to use. Defaults to env var CLAUDE_MODEL or claude-sonnet-4-6.
        max_turns: Maximum number of agent turns.
        max_budget_usd: Cost cap per session in USD.
        max_thinking_tokens: Max tokens for extended thinking. None = SDK default.
        env: Extra environment variables to pass to the agent subprocess.
        permission_mode: Agent permission mode (default: bypassPermissions).
        setting_sources: Where to load .claude/ settings from (default: ["project"]).
        on_assistant_text: Callback for each assistant text block.
        on_thinking: Callback for each thinking block.
        on_tool_use: Callback for each tool use (tool_name, input_dict).
        on_stderr: Callback for agent subprocess stderr lines.

    Returns:
        AgentResult with the agent's text output, cost, thinking, and metadata.
    """
    resolved_cwd = str(Path(cwd).resolve()) if cwd else None
    resolved_model = model or resolve_model()

    # Build environment: provider credentials + user env
    agent_env = resolve_provider_env(model=resolved_model)
    if env:
        agent_env.update(env)

    # Stderr handler
    def _on_stderr(line: str):
        line = line.rstrip()
        if line:
            if on_stderr:
                on_stderr(line)
            else:
                logger.warning("Agent stderr: %s", line)

    options_kwargs = dict(
        model=resolved_model,
        permission_mode=permission_mode,
        cwd=resolved_cwd,
        env=agent_env,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        setting_sources=setting_sources or ["project"],
        stderr=_on_stderr,
    )
    if mcp_servers is not None:
        options_kwargs["mcp_servers"] = mcp_servers
    blocked = list(disallowed_tools or [])
    if not allow_web:
        for t in ("WebSearch", "WebFetch"):
            if t not in blocked:
                blocked.append(t)
    if blocked:
        options_kwargs["disallowed_tools"] = blocked
    options = ClaudeAgentOptions(**options_kwargs)
    if max_thinking_tokens is not None:
        options.max_thinking_tokens = max_thinking_tokens

    # Run async query in sync context
    try:
        asyncio.get_running_loop()
        # Already in an async context — run in a separate thread
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(
                asyncio.run,
                _run_agent_async(prompt, options, on_assistant_text, on_thinking, on_tool_use),
            ).result()
    except RuntimeError:
        # No event loop running — use asyncio.run() directly
        return asyncio.run(
            _run_agent_async(prompt, options, on_assistant_text, on_thinking, on_tool_use)
        )


async def _run_agent_async(
    prompt: str,
    options: ClaudeAgentOptions,
    on_assistant_text: Callable[[str], None] | None,
    on_thinking: Callable[[str], None] | None,
    on_tool_use: Callable[[str, dict], None] | None,
) -> AgentResult:
    """Internal async implementation of the agent message loop."""
    cost = 0.0
    turns = 0
    result_text = ""
    is_error = False
    duration_ms = 0
    session_id = ""
    thinking_blocks: list[str] = []

    logger.info(
        "Starting agent: model=%s, max_turns=%d, budget=$%.2f",
        options.model, options.max_turns, options.max_budget_usd,
    )

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    # Thinking block (extended thinking / chain-of-thought)
                    if hasattr(block, "thinking") and block.thinking:
                        thinking_blocks.append(block.thinking)
                        if on_thinking:
                            on_thinking(block.thinking)
                        logger.info("Thinking: %s", block.thinking[:500])
                    # Text block
                    elif hasattr(block, "text") and block.text:
                        if on_assistant_text:
                            on_assistant_text(block.text)
                        logger.debug("Assistant: %s", block.text[:200])
                    # Tool use block
                    elif hasattr(block, "name"):
                        tool_input = getattr(block, "input", {}) or {}
                        if on_tool_use:
                            on_tool_use(block.name, tool_input)
                        detail = (
                            tool_input.get("command")
                            or tool_input.get("file_path")
                            or tool_input.get("pattern")
                            or tool_input.get("skill")
                            or str(tool_input)[:200]
                        )
                        logger.info("Tool use: %s → %s", block.name, detail)
            elif isinstance(message, SystemMessage):
                logger.debug(
                    "System: subtype=%s data=%s",
                    message.subtype, str(message.data)[:200],
                )
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                turns = message.num_turns
                is_error = message.is_error
                result_text = message.result or ""
                duration_ms = message.duration_ms
                session_id = getattr(message, "session_id", "")
                logger.info(
                    "Agent finished: turns=%d, cost=$%.4f, is_error=%s, duration=%dms",
                    turns, cost, is_error, duration_ms,
                )
    except Exception as e:
        logger.exception("Agent exception after %d turns, cost=$%.4f", turns, cost)
        return AgentResult(
            result=str(e),
            cost_usd=cost,
            turns=turns,
            is_error=True,
            thinking=thinking_blocks,
        )

    return AgentResult(
        result=result_text,
        cost_usd=cost,
        turns=turns,
        duration_ms=duration_ms,
        session_id=session_id,
        is_error=is_error,
        thinking=thinking_blocks,
    )
