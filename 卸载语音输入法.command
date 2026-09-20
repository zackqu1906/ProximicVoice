#!/bin/zsh
set -euo pipefail
TASK_ROOT="${0:A:h}"
if [[ ! -x "$TASK_ROOT/.build/input-method/InputMethodAdmin" ]]; then
  "$TASK_ROOT/scripts/build-input-method.sh"
fi
exec "$TASK_ROOT/.runtime/venv/bin/python" -B "$TASK_ROOT/scripts/install-input-method.py" uninstall
