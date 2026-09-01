#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPUS_VERSION="1.5.2"
OPUS_SHA256="65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1"
DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-12.0}"
OPUS_ROOT="$PROJECT_ROOT/.runtime/opus"
OPUS_DYLIB="$OPUS_ROOT/lib/libopus.0.dylib"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "The macOS Opus runtime must be built on Apple Silicon." >&2
    exit 1
fi
if ! xcrun --find clang >/dev/null 2>&1; then
    echo "Xcode Command Line Tools are required. Run: xcode-select --install" >&2
    exit 1
fi

BUILD_ROOT="$(/usr/bin/mktemp -d /private/tmp/proximic-opus.XXXXXX)"
cleanup() {
    case "$BUILD_ROOT" in
        /private/tmp/proximic-opus.*) rm -rf "$BUILD_ROOT" ;;
    esac
}
trap cleanup EXIT

ARCHIVE="$BUILD_ROOT/opus-$OPUS_VERSION.tar.gz"
curl -fL "https://downloads.xiph.org/releases/opus/opus-$OPUS_VERSION.tar.gz" \
    -o "$ARCHIVE"
ACTUAL_SHA256="$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$OPUS_SHA256" ]]; then
    echo "Opus source checksum mismatch: $ACTUAL_SHA256" >&2
    exit 1
fi

tar -xzf "$ARCHIVE" -C "$BUILD_ROOT"
cd "$BUILD_ROOT/opus-$OPUS_VERSION"
env \
    MACOSX_DEPLOYMENT_TARGET="$DEPLOYMENT_TARGET" \
    CFLAGS="-O2 -mmacosx-version-min=$DEPLOYMENT_TARGET" \
    LDFLAGS="-mmacosx-version-min=$DEPLOYMENT_TARGET" \
    ./configure \
        --prefix="$OPUS_ROOT" \
        --disable-static \
        --enable-shared \
        --disable-extra-programs \
        --disable-doc
make -j"$(sysctl -n hw.logicalcpu)"
make install
/usr/bin/install -m 644 COPYING "$OPUS_ROOT/COPYING.libopus"

install_name_tool -id "@rpath/libopus.0.dylib" "$OPUS_DYLIB"
if ! file "$OPUS_DYLIB" | grep -q "arm64"; then
    echo "Built libopus is not arm64: $OPUS_DYLIB" >&2
    exit 1
fi
if ! otool -l "$OPUS_DYLIB" | grep -A4 LC_BUILD_VERSION | grep -q "minos $DEPLOYMENT_TARGET"; then
    echo "Built libopus does not target macOS $DEPLOYMENT_TARGET." >&2
    exit 1
fi
echo "Opus: $OPUS_DYLIB"
