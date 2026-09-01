#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "The macOS package must be built on Apple Silicon." >&2
    exit 1
fi
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-15.0}"
REQUIRE_SIGNED_RELEASE="${PROXIMIC_REQUIRE_SIGNED_RELEASE:-0}"
if [[ "$REQUIRE_SIGNED_RELEASE" == "1" ]]; then
    if [[ -z "${APPLE_SIGNING_IDENTITY:-}" || -z "${APPLE_NOTARY_PROFILE:-}" ]]; then
        echo "A release build requires APPLE_SIGNING_IDENTITY and APPLE_NOTARY_PROFILE." >&2
        exit 1
    fi
fi
OPUS_DYLIB="$PROJECT_ROOT/.runtime/opus/lib/libopus.0.dylib"
if [[ ! -f "$OPUS_DYLIB" ]]; then
    MACOSX_DEPLOYMENT_TARGET=12.0 "$PROJECT_ROOT/scripts/install-opus-macos.sh"
fi
export PROXIMIC_OPUS_DYLIB="$OPUS_DYLIB"

if [[ -n "${PROXIMIC_PYTHON:-}" ]]; then
    PYTHON_BIN="$PROXIMIC_PYTHON"
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
else
    "$PROJECT_ROOT/scripts/install-python-macos.sh"
    PYTHON_BIN="$PROJECT_ROOT/.runtime/python-bin/python3.11"
fi
VENV_ROOT="$PROJECT_ROOT/.build/packaging-venv"
[[ -x "$VENV_ROOT/bin/python" ]] || "$PYTHON_BIN" -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"
"$PYTHON" -m pip install --upgrade \
    "pip==26.2.1" "setuptools==81.0.0" "wheel==0.48.0"
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
    echo "warning: generated an ad-hoc signed app; it is for local testing only." >&2
fi

codesign --verify --deep --strict --verbose=2 "$APP"
plutil -lint "$APP/Contents/Info.plist"
"$PYTHON" tools/verify_macos_bundle.py "$APP" --minimum-macos 15.0
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
PROXIMIC_DATA_HOME="$SMOKE_DATA_ROOT" \
    "$APP_EXECUTABLE" --self-check-package
SMOKE_LOG="$SMOKE_DATA_ROOT/logs/startup.log"
if [[ ! -f "$SMOKE_LOG" ]] \
    || ! grep -q "bundled Opus decoder ready" "$SMOKE_LOG" \
    || ! grep -q "bundled QML files ready" "$SMOKE_LOG" \
    || ! grep -q "QML root window ready" "$SMOKE_LOG" \
    || ! grep -q "packaged ASR imports ready" "$SMOKE_LOG"; then
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
    xcrun stapler validate "$DMG"
elif [[ -n "${APPLE_SIGNING_IDENTITY:-}" || -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
    echo "warning: both APPLE_SIGNING_IDENTITY and APPLE_NOTARY_PROFILE are required for notarization." >&2
fi

echo "DMG: $DMG"
