#!/bin/zsh
set -euo pipefail
TASK_ROOT="${0:A:h}"
"$TASK_ROOT/scripts/build-input-method.sh"
exec "$TASK_ROOT/.runtime/venv/bin/python" -B "$TASK_ROOT/scripts/install-input-method.py" install
