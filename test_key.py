"""Debug: print resolved config and test API call."""

import os
from claude_agent_trading.providers import resolve_provider_env, resolve_model

env = resolve_provider_env()
model = resolve_model()

print("=== Resolved Config ===")
print(f"Model: {model}")
for k, v in env.items():
    print(f"{k}={v}")
print()

# Direct API call test (not through Agent SDK)
import anthropic

api_key = env.get("ANTHROPIC_API_KEY")
base_url = env.get("ANTHROPIC_BASE_URL")

print("=== API Call Test ===")
print(f"api_key: {api_key}")
print(f"base_url: {base_url or '(default)'}")
print(f"model: {model}")
print()

kwargs = {"api_key": api_key}
if base_url:
    kwargs["base_url"] = base_url
client = anthropic.Anthropic(**kwargs)

try:
    resp = client.messages.create(
        model=model,
        max_tokens=50,
        messages=[{"role": "user", "content": "Say hello in one word"}],
    )
    print(f"Response: {resp.content[0].text}")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")
