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

codesign --verify --deep --strict --verbose=2 "$APP"
plutil -lint "$APP/Contents/Info.plist"
APP_EXECUTABLE="$APP/Contents/MacOS/ProximicVoice"
if ! file "$APP_EXECUTABLE" | grep -q "arm64"; then
    echo "Packaged executable is not Apple Silicon arm64: $APP_EXECUTABLE" >&2
    exit 1
fi

# Building a DMG proves only that files were collected.  Start the frozen app
# on the macOS builder as well so platform-only imports, native libraries and
# missing QML modules fail the build instead of failing on the user's Mac.
SMOKE_DATA_ROOT="$PROJECT_ROOT/.build/macos-smoke-data"
rm -rf "$SMOKE_DATA_ROOT"
mkdir -p "$SMOKE_DATA_ROOT"
QT_QPA_PLATFORM=offscreen \
PROXIMIC_DATA_HOME="$SMOKE_DATA_ROOT" \
PROXIMIC_STARTUP_PROBE=1 \
    "$APP_EXECUTABLE"
SMOKE_LOG="$SMOKE_DATA_ROOT/logs/startup.log"
if [[ ! -f "$SMOKE_LOG" ]] || ! grep -q "QML root window ready" "$SMOKE_LOG"; then
    echo "macOS packaged application did not complete its startup probe." >&2
    [[ -f "$SMOKE_LOG" ]] && cat "$SMOKE_LOG" >&2
    exit 1
fi
echo "macOS packaged startup probe passed."

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
