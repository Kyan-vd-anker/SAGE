#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${SAGE_DATA:-$ROOT/data}"
CACHE_DIR="${SAGE_LLAMA_CPP_CACHE_DIR:-$DATA_DIR/kv-cache}"
SERVER_BIN="${SAGE_LLAMA_CPP_SERVER_BIN:-llama-server}"
MODEL_PATH="${SAGE_LLAMA_CPP_MODEL_PATH:-}"

if ! command -v "$SERVER_BIN" >/dev/null 2>&1; then
    printf 'llama.cpp server binary not found: %s\n' "$SERVER_BIN" >&2
    exit 1
fi
if [[ -z "$MODEL_PATH" || ! -f "$MODEL_PATH" ]]; then
    printf 'Set SAGE_LLAMA_CPP_MODEL_PATH to a readable GGUF model file.\n' >&2
    exit 1
fi

umask 077
mkdir -p "$CACHE_DIR"
chmod 700 "$CACHE_DIR"

ARGS=(
    --model "$MODEL_PATH" \
    --host "${SAGE_LLAMA_CPP_HOST:-127.0.0.1}" \
    --port "${SAGE_LLAMA_CPP_PORT:-8080}" \
    --ctx-size "${SAGE_NUM_CTX:-65536}" \
    --threads "${SAGE_LLAMA_CPP_THREADS:-6}" \
    --parallel 1 \
    --cache-prompt \
    --slot-save-path "$CACHE_DIR" \
    --jinja
)
if [[ -n "${SAGE_LLAMA_CPP_API_KEY:-}" ]]; then
    ARGS+=(--api-key "$SAGE_LLAMA_CPP_API_KEY")
fi

exec "$SERVER_BIN" "${ARGS[@]}"
