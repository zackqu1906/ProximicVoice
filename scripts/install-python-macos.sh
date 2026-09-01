#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_VERSION="0.12.8"
UV_INSTALLER_SHA256="9d360fe3d4a2c26157ecab7b44892acfe6b43e490c940ed0b29ab12e2085b1d3"
UV_ROOT="$PROJECT_ROOT/.tools/uv"
UV="$UV_ROOT/uv"
PYTHON_ROOT="$PROJECT_ROOT/.runtime/python"
PYTHON_BIN_ROOT="$PROJECT_ROOT/.runtime/python-bin"
PYTHON="$PYTHON_BIN_ROOT/python3.11"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "The project-local Python bootstrap supports Apple Silicon macOS only." >&2
    exit 1
fi

if [[ ! -x "$UV" ]]; then
    INSTALLER="$(/usr/bin/mktemp /private/tmp/proximic-uv-install.XXXXXX)"
    cleanup() {
        case "$INSTALLER" in
            /private/tmp/proximic-uv-install.*) rm -f "$INSTALLER" ;;
        esac
    }
    trap cleanup EXIT
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" -o "$INSTALLER"
    ACTUAL_SHA256="$(/usr/bin/shasum -a 256 "$INSTALLER" | /usr/bin/awk '{print $1}')"
    if [[ "$ACTUAL_SHA256" != "$UV_INSTALLER_SHA256" ]]; then
        echo "uv installer checksum mismatch: $ACTUAL_SHA256" >&2
        exit 1
    fi
    env UV_UNMANAGED_INSTALL="$UV_ROOT" /bin/sh "$INSTALLER"
fi

env \
    UV_PYTHON_INSTALL_DIR="$PYTHON_ROOT" \
    UV_PYTHON_BIN_DIR="$PYTHON_BIN_ROOT" \
    UV_CACHE_DIR="$PROJECT_ROOT/.cache/uv" \
    "$UV" python install 3.11

"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))'
echo "Python: $PYTHON"
