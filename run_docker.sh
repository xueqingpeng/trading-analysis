#!/usr/bin/env bash
# Run trading-analysis benchmark inside Docker.
#
# 用户直接用 host 路径调子命令；脚本自动识别路径参数、挂载、重写为容器路径。
#
# 示例：
#   export ANTHROPIC_API_KEY=sk-proj-xxxx
#
#   # 方式 A：--model 作为 CLI 参数（单次调用级，优先）
#   ./run_docker.sh trading \
#       --symbol TSLA --start 2025-01-01 --end 2025-03-31 \
#       --db-path ./data/trading.duckdb \
#       --output  ./results/trading \
#       --model gpt-4.1 --max-turns 30 --max-budget 1.0
#
#   # 方式 B：通过环境变量（会被自动 -e 注入容器）
#   export CLAUDE_MODEL=gpt-4.1                          # 主模型；providers.py 会把
#                                                        # haiku/sonnet/opus/subagent
#                                                        # 全部统一到这个模型
#   ./run_docker.sh trading \
#       --symbol TSLA --start 2025-01-01 --end 2025-03-31 \
#       --db-path ./data/trading.duckdb \
#       --output  ./results/trading
#
#   ./run_docker.sh report-generation \
#       --benchmark-root ./financial_agentic_benchmark \
#       --ticker TSLA --output-root ./results/reports \
#       --model gpt-4.1
#
#   ./run_docker.sh --build -- --help
#   ./run_docker.sh bash
#
# 自动识别并翻译的路径参数：
#   输入目录 (ro): --benchmark-root  --data-root  --reports-root
#   输入文件 (ro): --tasks-file           (挂父目录)
#   DB 文件   (rw): --db-path             (挂父目录，DuckDB 会写 .wal)
#   输出目录 (rw): --output  --output-root (不存在则 mkdir)
#
# 自动透传（如 host 已 export）的环境变量：
#   ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, CLAUDE_MODEL,
#   ANTHROPIC_FOUNDRY_RESOURCE, ANTHROPIC_FOUNDRY_API_KEY,
#   CLAUDE_CODE_USE_FOUNDRY, AZURE_API_VERSION, PROXY_PORT
# 注：haiku/sonnet/opus/subagent 别名模型由 providers.py 自动设置成主 CLAUDE_MODEL，
# 不需要用户再 export ANTHROPIC_DEFAULT_* 系列。

set -euo pipefail

IMAGE="${IMAGE:-trading-analysis:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"

DO_BUILD=0
if [[ "${1:-}" == "--build" ]]; then
    DO_BUILD=1
    shift
fi
if [[ "${1:-}" == "--" ]]; then
    shift
fi

if [[ "$DO_BUILD" == "1" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[run_docker] building $IMAGE" >&2
    docker build -f "$HERE/docker/Dockerfile" -t "$IMAGE" "$HERE"
fi

INPUT_DIR_ARGS=(--benchmark-root --data-root --reports-root)
INPUT_FILE_ARGS=(--tasks-file)
DB_FILE_ARGS=(--db-path)
OUTPUT_DIR_ARGS=(--output --output-root)

in_list() {
    local needle="$1"; shift
    local x
    for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
    return 1
}

MOUNTS=()
declare -A SLOT_FOR
SLOT_IDX=0
# First host output dir we see (from --output / --output-root). Used as the
# destination for the run log file. Empty → no output dir → skip log capture.
OUTPUT_HOST_DIR=""
# Return values are written to these globals because command substitution
# $() would run in a subshell and lose MOUNTS / SLOT_FOR / SLOT_IDX updates.
PLAN_RESULT=""
REWRITE_RESULT=""

plan_mount() {
    local host_src="$1" mode="$2"
    local key="${host_src}|${mode}"
    if [[ -n "${SLOT_FOR[$key]:-}" ]]; then
        PLAN_RESULT="${SLOT_FOR[$key]}"
        return
    fi
    PLAN_RESULT="/io/slot${SLOT_IDX}"
    SLOT_IDX=$((SLOT_IDX + 1))
    SLOT_FOR[$key]="$PLAN_RESULT"
    if [[ "$mode" == "ro" ]]; then
        MOUNTS+=(-v "${host_src}:${PLAN_RESULT}:ro")
    else
        MOUNTS+=(-v "${host_src}:${PLAN_RESULT}")
    fi
}

rewrite_path() {
    local host_path="$1" kind="$2" mode="$3"
    local abs
    abs="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$host_path")"

    if [[ "$kind" == "dir" ]]; then
        if [[ "$mode" == "rw" ]]; then
            mkdir -p "$abs"
            # First writable output dir wins as the log destination
            [[ -z "$OUTPUT_HOST_DIR" ]] && OUTPUT_HOST_DIR="$abs"
        elif [[ ! -d "$abs" ]]; then
            echo "[run_docker] error: input directory not found: $abs" >&2
            exit 2
        fi
        plan_mount "$abs" "$mode"
        REWRITE_RESULT="$PLAN_RESULT"
    else
        if [[ "$mode" == "ro" && ! -f "$abs" ]]; then
            echo "[run_docker] error: input file not found: $abs" >&2
            exit 2
        fi
        local parent base
        parent="$(dirname "$abs")"
        base="$(basename "$abs")"
        [[ -d "$parent" ]] || mkdir -p "$parent"
        plan_mount "$parent" "$mode"
        REWRITE_RESULT="${PLAN_RESULT}/${base}"
    fi
}

classify() {
    local key="$1"
    if in_list "$key" "${INPUT_DIR_ARGS[@]}";  then echo "dir ro"; return; fi
    if in_list "$key" "${OUTPUT_DIR_ARGS[@]}"; then echo "dir rw"; return; fi
    if in_list "$key" "${INPUT_FILE_ARGS[@]}"; then echo "file_parent ro"; return; fi
    if in_list "$key" "${DB_FILE_ARGS[@]}";    then echo "file_parent rw"; return; fi
}

REWRITTEN=()
argv=("$@")
i=0
while (( i < ${#argv[@]} )); do
    a="${argv[i]}"
    key=""; val=""; has_eq=0
    if [[ "$a" == --*=* ]]; then
        key="${a%%=*}"; val="${a#*=}"; has_eq=1
    elif [[ "$a" == --* ]]; then
        key="$a"
    fi

    cls=""
    [[ -n "$key" ]] && cls="$(classify "$key")"
    if [[ -n "$cls" ]]; then
        read -r kind mode <<< "$cls"
        if (( has_eq == 1 )); then
            rewrite_path "$val" "$kind" "$mode"
            REWRITTEN+=("${key}=${REWRITE_RESULT}")
            i=$((i + 1))
        else
            if (( i + 1 >= ${#argv[@]} )); then
                echo "[run_docker] error: $key expects a value" >&2
                exit 2
            fi
            val="${argv[i+1]}"
            rewrite_path "$val" "$kind" "$mode"
            REWRITTEN+=("$key" "$REWRITE_RESULT")
            i=$((i + 2))
        fi
    else
        REWRITTEN+=("$a")
        i=$((i + 1))
    fi
done

PASS_ENVS=(
    ANTHROPIC_API_KEY ANTHROPIC_BASE_URL CLAUDE_MODEL
    ANTHROPIC_FOUNDRY_RESOURCE ANTHROPIC_FOUNDRY_API_KEY CLAUDE_CODE_USE_FOUNDRY
    AZURE_API_VERSION PROXY_PORT PROXY_VERBOSE
)
ENV_ARGS=()
for var in "${PASS_ENVS[@]}"; do
    if [[ -n "${!var:-}" ]]; then
        ENV_ARGS+=( -e "$var=${!var}" )
    fi
done
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_FOUNDRY_API_KEY:-}" ]]; then
    echo "[run_docker] WARNING: neither ANTHROPIC_API_KEY nor ANTHROPIC_FOUNDRY_API_KEY is set." >&2
fi

# 以 host UID/GID 运行容器：
#   1) claude CLI 拒绝以 root 身份使用 --dangerously-skip-permissions（SDK 强制传这个 flag）
#   2) 让 --output / --db-path 产生的文件在 host 上归当前用户所有
# HOME=/tmp：容器里没有 host 用户的 /etc/passwd 条目，claude CLI 要写缓存需要一个可写的 HOME
USER_FLAG=( -u "$(id -u):$(id -g)" -e HOME=/tmp )

# 调试 shell 模式下 (./run_docker.sh bash)：保留 -it，不 tee（pipe 会破坏交互 TTY）
IS_DEBUG=0
if [[ "${REWRITTEN[0]:-}" == "bash" || "${REWRITTEN[0]:-}" == "sh" ]]; then
    IS_DEBUG=1
fi

TTY_FLAG=()
if [[ "$IS_DEBUG" == "1" && -t 0 && -t 1 ]]; then
    TTY_FLAG=( -it )
fi

if [[ "$IS_DEBUG" == "1" || -z "$OUTPUT_HOST_DIR" ]]; then
    exec docker run --rm "${TTY_FLAG[@]}" \
        "${USER_FLAG[@]}" \
        "${ENV_ARGS[@]}" \
        "${MOUNTS[@]}" \
        "$IMAGE" \
        "${REWRITTEN[@]}"
fi

# Normal benchmark run: tee all container output to {OUTPUT_HOST_DIR}/run_<ts>.log
LOG_FILE="${OUTPUT_HOST_DIR}/run_$(date -u +%Y%m%dT%H%M%SZ).log"
echo "[run_docker] saving full log → $LOG_FILE" >&2

docker run --rm \
    "${USER_FLAG[@]}" \
    "${ENV_ARGS[@]}" \
    "${MOUNTS[@]}" \
    "$IMAGE" \
    "${REWRITTEN[@]}" 2>&1 | tee -a "$LOG_FILE"
exit "${PIPESTATUS[0]}"
