"""
Claude API → OpenAI-compatible API Proxy

Receives Claude /v1/messages requests, translates them to OpenAI /v1/chat/completions format,
and translates the streaming response back to Claude SSE format.

Supports OpenAI / Gemini / Azure OpenAI — auto-detects provider based on API key prefix.
Proxy is a pure forwarder: model selection and API key are provided by the client.

Usage:
    python proxy.py

    # Client sets ANTHROPIC_API_KEY depending on provider:
    #   OpenAI:  sk-...
    #   Gemini:  AIzaSy...
    #   Azure:   azure:<endpoint>:<api-key>
    #            e.g. azure:https://myres.openai.azure.com:abcdef123456
"""

import json
import os
import uuid
from dataclasses import dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

# ─── Config ───────────────────────────────────────────────────────────────────

PROXY_PORT = int(os.environ.get("PROXY_PORT", "18080"))

AZURE_API_VERSION = os.environ.get("AZURE_API_VERSION", "2024-12-01-preview")


@dataclass
class ProviderInfo:
    name: str
    api_key: str
    base_url: str
    auth_header: str = "Authorization"   # header name for auth
    auth_prefix: str = "Bearer "         # prefix before the key value

    def chat_completions_url(self, model: str) -> str:
        """Build the full chat/completions URL (Azure needs model in path)."""
        if self.name == "azure":
            return (f"{self.base_url}/openai/deployments/{model}"
                    f"/chat/completions?api-version={AZURE_API_VERSION}")
        return f"{self.base_url}/chat/completions"

    def request_headers(self) -> dict:
        return {
            self.auth_header: f"{self.auth_prefix}{self.api_key}",
            "Content-Type": "application/json",
        }


def detect_provider(raw_key: str) -> ProviderInfo:
    """Detect provider by API key prefix and return routing info."""
    # Azure: composite key format "azure:<endpoint>:<api-key>"
    if raw_key.startswith("azure:"):
        parts = raw_key.split(":", 2)  # ["azure", "<endpoint>", "<key>"]
        if len(parts) == 3:
            _, endpoint, actual_key = parts
            return ProviderInfo(
                name="azure",
                api_key=actual_key,
                base_url=endpoint.rstrip("/"),
                auth_header="api-key",
                auth_prefix="",
            )

    # OpenAI
    if raw_key.startswith("sk-"):
        return ProviderInfo(
            name="openai", api_key=raw_key,
            base_url="https://api.openai.com/v1",
        )

    # Gemini
    if raw_key.startswith("AIzaSy"):
        return ProviderInfo(
            name="gemini", api_key=raw_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        )

    # Unknown, default to OpenAI
    return ProviderInfo(
        name="unknown", api_key=raw_key,
        base_url="https://api.openai.com/v1",
    )

app = FastAPI()


# ─── Schema Sanitization ─────────────────────────────────────────────────────
# Ref: claudish transform.ts:removeUriFormat + openai-tools.ts:sanitizeSchemaForOpenAI


def remove_uri_format(schema):
    """Recursively remove format:"uri" from JSON Schema (unsupported by OpenAI)."""
    if schema is None or not isinstance(schema, dict):
        return schema
    if isinstance(schema, list):
        return [remove_uri_format(item) for item in schema]

    if schema.get("type") == "string" and schema.get("format") == "uri":
        return {k: v for k, v in schema.items() if k != "format"}

    result = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {pk: remove_uri_format(pv) for pk, pv in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = remove_uri_format(value)
        elif key == "additionalProperties" and isinstance(value, dict):
            result[key] = remove_uri_format(value)
        elif key in ("anyOf", "allOf", "oneOf") and isinstance(value, list):
            result[key] = [remove_uri_format(item) for item in value]
        else:
            result[key] = value
    return result


def sanitize_schema(schema):
    """Sanitize tool input_schema for OpenAI function calling compatibility."""
    if not schema or not isinstance(schema, dict):
        return remove_uri_format(schema)

    root = dict(schema)

    # Collapse top-level oneOf/anyOf/allOf
    for combiner in ("oneOf", "anyOf", "allOf"):
        branches = root.get(combiner)
        if isinstance(branches, list) and len(branches) > 0:
            obj_branch = next(
                (b for b in branches if isinstance(b, dict) and b.get("type") == "object"),
                None,
            )
            if obj_branch:
                del root[combiner]
                root.update(obj_branch)
            else:
                root = {"type": "object", "properties": {}, "additionalProperties": True}
            break

    root.pop("enum", None)
    root.pop("not", None)
    root["type"] = "object"

    return remove_uri_format(root)


# ─── Request Translation ─────────────────────────────────────────────────────
# Ref: claudish openai-messages.ts, openai-tools.ts, openai-api-format.ts


def convert_system(body):
    """Claude system array → OpenAI system message string."""
    sys = body.get("system")
    if not sys:
        return []

    if isinstance(sys, list):
        parts = []
        for item in sys:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        content = "\n\n".join(parts)
    else:
        content = str(sys)

    return [{"role": "system", "content": content}]


def convert_user_message(msg):
    """Claude user message → OpenAI messages (may produce multiple due to tool_result splitting)."""
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    if not isinstance(content, list):
        return [{"role": "user", "content": str(content)}]

    content_parts = []
    tool_results = []
    seen = set()

    for block in content:
        block_type = block.get("type")

        if block_type == "text":
            content_parts.append({"type": "text", "text": block["text"]})

        elif block_type == "image":
            source = block.get("source", {})
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            })

        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            if tool_use_id in seen:
                continue
            seen.add(tool_use_id)

            result_content = block.get("content", "")
            if not isinstance(result_content, str):
                result_content = json.dumps(result_content)

            tool_results.append({
                "role": "tool",
                "content": result_content,
                "tool_call_id": tool_use_id,
            })

    # Tool results first, then user message (ref: claudish line 93)
    messages = []
    if tool_results:
        messages.extend(tool_results)
    if content_parts:
        messages.append({"role": "user", "content": content_parts})
    return messages


def convert_assistant_message(msg):
    """Claude assistant message → OpenAI assistant message."""
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]

    if not isinstance(content, list):
        return [{"role": "assistant", "content": str(content)}]

    strings = []
    tool_calls = []
    seen = set()

    for block in content:
        block_type = block.get("type")

        if block_type == "text":
            strings.append(block["text"])

        elif block_type == "thinking":
            pass  # Discard thinking blocks

        elif block_type == "tool_use":
            block_id = block.get("id", "")
            if block_id in seen:
                continue
            seen.add(block_id)

            tool_calls.append({
                "id": block_id,
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    m = {"role": "assistant"}
    if strings:
        m["content"] = " ".join(strings)
    elif tool_calls:
        m["content"] = None

    if tool_calls:
        m["tool_calls"] = tool_calls

    if m.get("content") is not None or m.get("tool_calls"):
        return [m]
    return []


def convert_tools(body):
    """Claude tools → OpenAI function calling tools."""
    tools = body.get("tools")
    if not tools:
        return []

    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": sanitize_schema(tool.get("input_schema", {})),
            },
        }
        for tool in tools
    ]


def translate_request(body, model):
    """Full Claude → OpenAI request translation."""
    messages = convert_system(body)

    for msg in body.get("messages", []):
        role = msg.get("role")
        if role == "user":
            messages.extend(convert_user_message(msg))
        elif role == "assistant":
            messages.extend(convert_assistant_message(msg))

    tools = convert_tools(body)

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    if body.get("max_tokens"):
        # Newer OpenAI models require max_completion_tokens instead of max_tokens
        payload["max_completion_tokens"] = body["max_tokens"]

    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]

    if tools:
        payload["tools"] = tools

    # tool_choice translation
    tc = body.get("tool_choice")
    if tc:
        tc_type = tc.get("type") if isinstance(tc, dict) else tc
        tc_name = tc.get("name") if isinstance(tc, dict) else None
        if tc_type == "tool" and tc_name:
            payload["tool_choice"] = {"type": "function", "function": {"name": tc_name}}
        elif tc_type in ("auto", "none"):
            payload["tool_choice"] = tc_type

    return payload


# ─── Stream Response Translation ─────────────────────────────────────────────
# Ref: claudish openai-sse.ts state machine


@dataclass
class ToolState:
    id: str
    name: str
    block_index: int
    started: bool = False
    closed: bool = False
    arguments: str = ""


@dataclass
class StreamState:
    text_started: bool = False
    text_idx: int = -1
    cur_idx: int = 0
    tools: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    stop_reason: str = "end_turn"


def format_sse(event_type, data):
    """Format a Claude SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def translate_stream(response_lines, model_name):
    """Translate OpenAI SSE stream → Claude SSE stream."""
    state = StreamState()
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"

    yield format_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model_name,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield format_sse("ping", {"type": "ping"})

    async for line in response_lines:
        line = line.strip()
        if not line or not line.startswith("data: "):
            continue

        data_str = line[6:]
        if data_str == "[DONE]":
            break

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if chunk.get("usage"):
            state.usage = chunk["usage"]

        choices = chunk.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Text content
        text_content = delta.get("content")
        if text_content:
            if not state.text_started:
                state.text_idx = state.cur_idx
                state.cur_idx += 1
                yield format_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": state.text_idx,
                    "content_block": {"type": "text", "text": ""},
                })
                state.text_started = True

            yield format_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": state.text_idx,
                "delta": {"type": "text_delta", "text": text_content},
            })

        # Tool calls
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                idx = tc.get("index", 0)
                func = tc.get("function", {})
                tool_name = func.get("name")
                tool_args = func.get("arguments", "")

                if tool_name:
                    # Close text block before starting a tool
                    if state.text_started:
                        yield format_sse("content_block_stop", {
                            "type": "content_block_stop",
                            "index": state.text_idx,
                        })
                        state.text_started = False

                    tool_id = tc.get("id", f"tool_{uuid.uuid4().hex[:12]}")
                    block_idx = state.cur_idx
                    state.cur_idx += 1

                    t = ToolState(
                        id=tool_id,
                        name=tool_name,
                        block_index=block_idx,
                        started=True,
                    )
                    state.tools[idx] = t

                    yield format_sse("content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name},
                    })

                if tool_args and idx in state.tools:
                    t = state.tools[idx]
                    t.arguments += tool_args
                    yield format_sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": t.block_index,
                        "delta": {"type": "input_json_delta", "partial_json": tool_args},
                    })

        if finish_reason:
            if finish_reason == "tool_calls":
                state.stop_reason = "tool_use"
            elif finish_reason == "length":
                state.stop_reason = "max_tokens"
            else:
                state.stop_reason = "end_turn"

    # Close all open blocks
    if state.text_started:
        yield format_sse("content_block_stop", {
            "type": "content_block_stop",
            "index": state.text_idx,
        })

    for t in state.tools.values():
        if t.started and not t.closed:
            yield format_sse("content_block_stop", {
                "type": "content_block_stop",
                "index": t.block_index,
            })
            t.closed = True

    output_tokens = state.usage.get("completion_tokens", 0) if state.usage else 0
    yield format_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": state.stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield format_sse("message_stop", {"type": "message_stop"})


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/")
async def health():
    return {"status": "ok", "proxy": "claude-to-openai-compatible"}


@app.post("/v1/messages")
async def handle_messages(request: Request):
    body = await request.json()

    # Read key and model from client request
    raw_key = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ")
        or ""
    )
    model = body.get("model", "")

    # Auto-detect provider by key prefix
    provider = detect_provider(raw_key)

    openai_payload = translate_request(body, model)
    url = provider.chat_completions_url(model)

    print(f"[proxy] {provider.name} | model={model} | "
          f"msgs={len(openai_payload['messages'])} | "
          f"tools={len(openai_payload.get('tools', []))} | "
          f"max_tokens={openai_payload.get('max_tokens')}",
          flush=True)

    async def stream_generator():
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", url,
                json=openai_payload,
                headers=provider.request_headers(),
                timeout=httpx.Timeout(300.0, connect=10.0),
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_msg = error_body.decode()[:500]
                    print(f"[proxy] {provider.name} error {resp.status_code}: {error_msg}")
                    yield format_sse("error", {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"{provider.name} error {resp.status_code}: {error_msg}",
                        },
                    })
                    return

                async for chunk in translate_stream(resp.aiter_lines(), model):
                    yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ─── Startup ──────────────────────────────────────────────────────────────────

SUPPORTED_PROVIDERS = ["openai", "gemini", "azure"]

if __name__ == "__main__":
    print(f"Claude → OpenAI-compatible proxy")
    print(f"  Listening: http://127.0.0.1:{PROXY_PORT}")
    print(f"  Providers: {', '.join(SUPPORTED_PROVIDERS)}")
    print(f"  Model & Key: provided by client, proxy auto-routes by key prefix")
    print()

    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="warning")
