#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.runtime/venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Proximic Voice is not installed yet." >&2
    echo "Run: ./scripts/setup-macos.sh" >&2
    exit 1
fi

export PIP_CACHE_DIR="$PROJECT_ROOT/.cache/pip"
export MODELSCOPE_CACHE="$PROJECT_ROOT/.cache/modelscope"
export HF_HOME="$PROJECT_ROOT/.cache/huggingface"
export TORCH_HOME="$PROJECT_ROOT/.cache/torch"

cd "$PROJECT_ROOT"
exec "$VENV_PYTHON" -m proximic_ring.ui "$@"
