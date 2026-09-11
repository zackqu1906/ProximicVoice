#!/usr/bin/env bash
set -euo pipefail
GESTURE_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GESTURE_PYTHON="$GESTURE_PROJECT_ROOT/.runtime/venv/bin/python"
if [[ ! -x "$GESTURE_PYTHON" ]]; then
    echo "未找到项目 Python，请先运行 scripts/setup-macos.sh。" >&2
    exit 1
fi
cd "$GESTURE_PROJECT_ROOT"
exec "$GESTURE_PYTHON" -u tools/test_host_gestures.py "$@"
