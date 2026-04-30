#!/usr/bin/env bash
set -euo pipefail

PROXY_PORT="${PROXY_PORT:-18080}"
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"

if [[ "${1:-}" == "bash" || "${1:-}" == "sh" ]]; then
    exec "$@"
fi

need_proxy() {
    if [[ "${CLAUDE_CODE_USE_FOUNDRY:-0}" == "1" ]]; then return 1; fi
    if [[ -n "${ANTHROPIC_BASE_URL:-}" ]]; then return 1; fi
    if [[ "${ANTHROPIC_API_KEY:-}" == sk-ant-* ]]; then return 1; fi
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then return 0; fi
    return 1
}

if need_proxy; then
    echo "[entrypoint] starting claude_proxy on ${PROXY_URL} ..." >&2
    PROXY_LOG="/tmp/claude_proxy.log"
    if [[ "${PROXY_VERBOSE:-0}" == "1" ]]; then
        python -m uvicorn claude_proxy.proxy:app \
            --host 127.0.0.1 --port "${PROXY_PORT}" \
            --log-level warning &
    else
        # Redirect proxy stdout/stderr to a log file so benchmark thinking
        # (--verbose) isn't buried under per-request access lines. Tail with
        # PROXY_VERBOSE=1 to keep proxy logs on stdout instead.
        python -m uvicorn claude_proxy.proxy:app \
            --host 127.0.0.1 --port "${PROXY_PORT}" \
            --log-level warning \
            >"${PROXY_LOG}" 2>&1 &
    fi
    PROXY_PID=$!
    trap 'kill "${PROXY_PID}" 2>/dev/null || true' EXIT INT TERM

    # Silently wait for uvicorn to bind the port (~15s cap). Once the socket
    # accepts connections, FastAPI is ready — no HTTP probe needed.
    if ! python3 - "$PROXY_PORT" "$PROXY_PID" <<'PY' >&2
import socket, sys, time, os, signal
port = int(sys.argv[1])
proxy_pid = int(sys.argv[2])
deadline = time.time() + 15.0
while time.time() < deadline:
    try:
        os.kill(proxy_pid, 0)
    except OSError:
        print("[entrypoint] proxy process died before becoming ready", file=sys.stderr)
        sys.exit(1)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            sys.exit(0)
    except OSError:
        time.sleep(0.1)
print("[entrypoint] proxy did not become ready within 15s", file=sys.stderr)
sys.exit(1)
PY
    then
        exit 1
    fi

    echo "[entrypoint] proxy ready" >&2
    if [[ "${PROXY_VERBOSE:-0}" != "1" ]]; then
        echo "[entrypoint] proxy log → ${PROXY_LOG}  (set PROXY_VERBOSE=1 to tail on stdout)" >&2
    fi
    export ANTHROPIC_BASE_URL="${PROXY_URL}"
fi

exec python /app/run_benchmark.py "$@"
