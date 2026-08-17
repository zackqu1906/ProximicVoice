#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "Proximic Voice currently supports Apple Silicon macOS only." >&2
    exit 1
fi

if [[ ! -d "$PROJECT_ROOT/third_party/streaming-sensevoice/streaming_sensevoice" ]]; then
    echo "Missing third_party/streaming-sensevoice. Clone or download the complete project." >&2
    exit 1
fi

PYTHON_BIN="${PROXIMIC_PYTHON:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3.11 is required. Install it with: brew install python@3.11" >&2
    echo "Or set PROXIMIC_PYTHON=/path/to/python3.11" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))'; then
    echo "PROXIMIC_PYTHON must point to Python 3.11." >&2
    exit 1
fi

RUNTIME_ROOT="$PROJECT_ROOT/.runtime"
VENV_ROOT="$RUNTIME_ROOT/venv"
VENV_PYTHON="$VENV_ROOT/bin/python"
CONSTRAINT_FILE="$PROJECT_ROOT/requirements-macos.lock"

if [[ ! -f "$CONSTRAINT_FILE" ]]; then
    echo "Missing requirements-macos.lock. Clone or download the complete project." >&2
    exit 1
fi

mkdir -p \
    "$PROJECT_ROOT/.cache/pip" \
    "$PROJECT_ROOT/.cache/modelscope" \
    "$PROJECT_ROOT/.cache/huggingface" \
    "$PROJECT_ROOT/.cache/torch"

export PIP_CACHE_DIR="$PROJECT_ROOT/.cache/pip"
export MODELSCOPE_CACHE="$PROJECT_ROOT/.cache/modelscope"
export HF_HOME="$PROJECT_ROOT/.cache/huggingface"
export TORCH_HOME="$PROJECT_ROOT/.cache/torch"

if [[ ! -x "$VENV_PYTHON" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_ROOT"
elif ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))'; then
    echo "Existing .runtime/venv does not use Python 3.11; remove it and run setup again." >&2
    exit 1
fi

"$VENV_PYTHON" -m pip install --upgrade "pip==26.2.1" "setuptools==81.0.0" "wheel==0.48.0"
"$VENV_PYTHON" -m pip install -c "$CONSTRAINT_FILE" -e ".[ring,asr-streaming-sensevoice,asr-funasr-nano,asr-volcengine,ui]"
"$VENV_PYTHON" -c 'import torch, torchaudio, PySide6, bleak, funasr, modelscope, transformers, websocket, asr_decoder, online_fbank, zhconv, pyopenjtalk, proximic_ring; import proximic_ring.ui.main; assert torch.version.cuda is None; print("Torch:", torch.__version__); print("Compute: cpu"); print("macOS installation self-check passed.")'

echo
echo "Installation completed. Start Proximic Voice with:"
echo "  ./scripts/start-ui.sh"
