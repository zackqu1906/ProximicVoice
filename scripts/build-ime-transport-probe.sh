#!/bin/zsh
set -euo pipefail
TASK_ROOT="${0:A:h:h}"
BUILD_ROOT="$TASK_ROOT/.build/input-method"
PROBE_BUNDLE="$BUILD_ROOT/IMETransportProbe.app"
mkdir -p "$BUILD_ROOT/module-cache" "$PROBE_BUNDLE/Contents/MacOS"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit \
  "$TASK_ROOT/native/ProxiMicInput/Tests/TransportProbe/main.swift" \
  -o "$PROBE_BUNDLE/Contents/MacOS/IMETransportProbe"
"$TASK_ROOT/.runtime/venv/bin/python" -B - "$PROBE_BUNDLE/Contents/Info.plist" <<'PY'
import plistlib
import sys
from pathlib import Path
Path(sys.argv[1]).write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'com.proximic.IMETransportProbe',
    'CFBundleExecutable': 'IMETransportProbe',
    'CFBundleName': 'ProxiMic IME Transport Probe',
    'CFBundlePackageType': 'APPL',
    'NSHighResolutionCapable': True,
}))
PY
codesign --force --sign - "$PROBE_BUNDLE"
print -r -- "已构建独立诊断窗口：$PROBE_BUNDLE"
print -r -- "先退出主程序，再运行 scripts/probe-ime-transport.py，并打开此窗口。输入法仍需手动选择。"
