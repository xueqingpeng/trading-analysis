#!/usr/bin/env python3
"""Pretty-print the Claude Code stream-json output for readable demos.

Reads `claude --print --output-format stream-json --verbose` events from
stdin and emits a terminal-friendly transcript with colored markers for:
  - session init (model, MCP servers)
  - assistant text streamed in real time
  - tool calls (name + inputs)
  - tool results (truncated)
  - final result (cost + duration)

Usage:
    claude --print --output-format stream-json --verbose ... | python3 _stream_format.py
"""

from __future__ import annotations

import json
import sys

# ANSI color helpers
RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
GREY = "\x1b[90m"


def _truncate(s: str, n: int = 110) -> str:
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# Suppress tool_result lines that are pure permission negotiation noise
_NOISE = (
    "haven't granted", "permission", "obfuscation",
    "Newline followed by", "Contains brace", "Unknown skill",
    "no project settings", "tool_use_error",
)


def _is_noise(text: str) -> bool:
    return any(n in text for n in _NOISE)


def _print_assistant_text(text: str) -> None:
    if not text:
        return
    sys.stdout.write(text)
    sys.stdout.flush()


def main() -> int:
    in_text_block = False
    in_tool_block = False
    current_tool_name = None
    current_tool_input = None
    seen_assistant = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type")

        if t == "system" and ev.get("subtype") == "init":
            model = ev.get("model", "?")
            mcp = ev.get("mcp_servers", []) or []
            mcp_str = ", ".join(
                f"{s['name']}({s['status']})" for s in mcp
            ) or "none"
            cwd = ev.get("cwd", "?")
            print(f"{DIM}╭─{RESET} {BOLD}claude code session{RESET} {DIM}─"
                  f"───────────────────────────────────────────────{RESET}")
            print(f"{DIM}│{RESET} model     : {model}")
            print(f"{DIM}│{RESET} cwd       : {cwd}")
            print(f"{DIM}│{RESET} mcp       : {mcp_str}")
            print(f"{DIM}╰────────────────────────────────────────────────"
                  f"───────────────{RESET}")
            print()

        elif t == "stream_event":
            event = ev.get("event", {})
            etype = event.get("type")

            if etype == "content_block_start":
                cb = event.get("content_block", {})
                if cb.get("type") == "text":
                    in_text_block = True
                    in_tool_block = False
                    if not seen_assistant:
                        seen_assistant = True
                    sys.stdout.write(f"\n{YELLOW}▎ {RESET}")
                    sys.stdout.flush()
                elif cb.get("type") == "tool_use":
                    in_tool_block = True
                    in_text_block = False
                    current_tool_name = cb.get("name", "?")
                    current_tool_input = ""

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                dt = delta.get("type")
                if dt == "text_delta":
                    _print_assistant_text(delta.get("text", ""))
                elif dt == "input_json_delta":
                    current_tool_input = (current_tool_input or "") + (
                        delta.get("partial_json", "") or "")

            elif etype == "content_block_stop":
                if in_tool_block and current_tool_name:
                    try:
                        args = json.loads(current_tool_input or "{}")
                    except Exception:
                        args = current_tool_input
                    args_str = json.dumps(args, ensure_ascii=False)
                    if len(args_str) > 200:
                        args_str = args_str[:200] + "…"
                    print(f"{CYAN}→ {current_tool_name}{RESET}{DIM}({args_str}){RESET}")
                    current_tool_name = None
                    current_tool_input = None
                in_text_block = False
                in_tool_block = False

            elif etype == "message_stop":
                pass

        elif t == "user":
            # tool_result(s) coming back from MCP / Bash
            msg = ev.get("message", {})
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        text = " ".join(
                            (c.get("text", "") if isinstance(c, dict) else str(c))
                            for c in content
                        )
                    else:
                        text = str(content) if content is not None else ""
                    is_error = block.get("is_error")
                    if _is_noise(text):
                        continue  # skip permission negotiation noise
                    color = MAGENTA if is_error else GREEN
                    arrow = "✗" if is_error else "←"
                    print(f"  {color}{arrow}{RESET} {DIM}{_truncate(text, 110)}{RESET}")

        elif t == "result":
            cost = ev.get("total_cost_usd", 0)
            dur = ev.get("duration_ms", 0) / 1000.0
            turns = ev.get("num_turns", 0)
            err = ev.get("is_error", False)
            color = MAGENTA if err else GREEN
            label = "FAILED" if err else "DONE"
            print()
            print(f"{DIM}╭─{RESET} {BOLD}{color}{label}{RESET} {DIM}─"
                  f"────────────────────────────────────────────────{RESET}")
            print(f"{DIM}│{RESET} turns     : {turns}")
            print(f"{DIM}│{RESET} duration  : {dur:.1f}s")
            print(f"{DIM}│{RESET} cost      : ${cost:.4f}")
            print(f"{DIM}╰────────────────────────────────────────────────"
                  f"───────────────{RESET}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
