#!/bin/zsh
set -e
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec /Users/admin/Downloads/ProximicVoice-main/.runtime/venv/bin/python "$SCRIPT_DIR/serve.py" --open
