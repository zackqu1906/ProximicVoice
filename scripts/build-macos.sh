#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "The macOS package must be built on Apple Silicon." >&2
    exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required to bundle libopus." >&2
    exit 1
fi
brew list opus >/dev/null 2>&1 || brew install opus

PYTHON_BIN="${PROXIMIC_PYTHON:-python3.11}"
VENV_ROOT="$PROJECT_ROOT/.build/packaging-venv"
[[ -x "$VENV_ROOT/bin/python" ]] || "$PYTHON_BIN" -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"
"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install -c requirements-macos.lock \
    ".[ring-opus,asr-streaming-sensevoice,asr-funasr-nano,asr-volcengine,ui]" \
    -r requirements-packaging.txt
"$PYTHON" -m PyInstaller --noconfirm --clean packaging/proximic_voice.spec

APP="$PROJECT_ROOT/dist/Proximic Voice.app"
if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
    codesign --force --deep --options runtime --timestamp \
        --sign "$APPLE_SIGNING_IDENTITY" "$APP"
else
    codesign --force --deep --sign - "$APP"
fi

DMG_ROOT="$PROJECT_ROOT/.build/dmg"
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
cp -R "$APP" "$DMG_ROOT/"
ln -s /Applications "$DMG_ROOT/Applications"
DMG="$PROJECT_ROOT/dist/ProximicVoice-0.6.0-macos-arm64.dmg"
rm -f "$DMG"
hdiutil create -volname "Proximic Voice" -srcfolder "$DMG_ROOT" \
    -ov -format UDZO "$DMG"

if [[ -n "${APPLE_NOTARY_PROFILE:-}" && -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
fi

echo "DMG: $DMG"
