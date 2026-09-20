#!/bin/zsh
set -euo pipefail
TASK_ROOT="${0:A:h:h}"
BUILD_ROOT="$TASK_ROOT/.build/input-method"
APP_BUNDLE="$BUILD_ROOT/ProxiMicInput.app"
mkdir -p "$BUILD_ROOT/module-cache" "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
export CLANG_MODULE_CACHE_PATH="$BUILD_ROOT/module-cache"
export SWIFT_MODULECACHE_PATH="$BUILD_ROOT/module-cache"
TARGET_ARCH="$(uname -m)"
DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-15.0}"
xcrun swiftc -swift-version 5 -O -module-cache-path "$BUILD_ROOT/module-cache" \
  -target "$TARGET_ARCH-apple-macosx$DEPLOYMENT_TARGET" \
  -framework AppKit -framework InputMethodKit -framework Carbon \
  "$TASK_ROOT"/native/ProxiMicInput/Sources/*.swift \
  -o "$APP_BUNDLE/Contents/MacOS/ProxiMicInput"
cp "$TASK_ROOT/native/ProxiMicInput/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
if [[ -d "$TASK_ROOT/native/ProxiMicInput/Resources" ]]; then
  ditto "$TASK_ROOT/native/ProxiMicInput/Resources" "$APP_BUNDLE/Contents/Resources"
fi
plutil -lint "$APP_BUNDLE/Contents/Info.plist"
codesign --force --sign "${PROXIMIC_SIGN_IDENTITY:--}" "$APP_BUNDLE"
codesign --verify --strict "$APP_BUNDLE"
xcrun swiftc -swift-version 5 -O -module-cache-path "$BUILD_ROOT/module-cache" \
  -target "$TARGET_ARCH-apple-macosx$DEPLOYMENT_TARGET" \
  -parse-as-library -framework Carbon -framework AppKit \
  "$TASK_ROOT/scripts/InputMethodAdmin.swift" "$TASK_ROOT/scripts/InputSourceSelection.swift" -o "$BUILD_ROOT/InputMethodAdmin"
codesign --force --sign "${PROXIMIC_SIGN_IDENTITY:--}" "$BUILD_ROOT/InputMethodAdmin"
codesign --verify --strict "$BUILD_ROOT/InputMethodAdmin"
print -r -- "已构建：$APP_BUNDLE"
print -r -- "尚未安装或切换输入法。"
