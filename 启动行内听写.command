#!/bin/zsh
set -eu
TASK_ROOT="${0:A:h}"
cd "$TASK_ROOT"
export PYTHONPATH="$TASK_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$TASK_ROOT/.runtime/venv/bin/python" -B -m proximic_ring.ui
