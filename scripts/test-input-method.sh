#!/bin/zsh
set -euo pipefail
TASK_ROOT="${0:A:h:h}"
BUILD_ROOT="$TASK_ROOT/.build/input-method"
mkdir -p "$BUILD_ROOT/module-cache"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit \
  "$TASK_ROOT/native/ProxiMicInput/Sources/ActionPanelAnchor.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/ActionPanelAnchor/main.swift" -o "$BUILD_ROOT/panel-anchor-tests"
"$BUILD_ROOT/panel-anchor-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/ActionPanelAnchor.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CaretDiagnostics.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/ActionPanelPresentation.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/ActionPanel.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/ActionPanel/main.swift" -o "$BUILD_ROOT/panel-presentation-tests"
"$BUILD_ROOT/panel-presentation-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/main.swift" -o "$BUILD_ROOT/transaction-tests"
"$BUILD_ROOT/transaction-tests"
xcrun swiftc -swift-version 5 -O -module-cache-path "$BUILD_ROOT/module-cache" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionUndoHistory.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/UndoHistory/main.swift" -o "$BUILD_ROOT/undo-history-tests"
"$BUILD_ROOT/undo-history-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/LifecycleReentry/main.swift" -o "$BUILD_ROOT/lifecycle-reentry-tests"
"$BUILD_ROOT/lifecycle-reentry-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionCommitDelivery.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/NativeCommit/main.swift" -o "$BUILD_ROOT/native-commit-tests"
"$BUILD_ROOT/native-commit-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" \
  -framework AppKit -framework InputMethodKit -framework Carbon \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionUndoHistory.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionHandoff.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionCommitDelivery.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CommittedReplacementDelivery.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/WeChatCompatibility.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/EditorContextReader.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CaretDiagnostics.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/InputClient.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/InputAdapter/main.swift" -o "$BUILD_ROOT/input-adapter-tests"
"$BUILD_ROOT/input-adapter-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionSession.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/CompositionHandoff.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/Handoff/main.swift" -o "$BUILD_ROOT/handoff-tests"
"$BUILD_ROOT/handoff-tests"
# Run the production activation callbacks against local, reentrant IMK clients.
ACTIVATION_SOURCES=("$TASK_ROOT"/native/ProxiMicInput/Sources/*.swift)
ACTIVATION_SOURCES=("${(@)ACTIVATION_SOURCES:#*/main.swift}")
ACTIVATION_SOURCES=("${(@)ACTIVATION_SOURCES:#*/ActionPanel.swift}")
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" \
  -framework AppKit -framework InputMethodKit -framework Carbon \
  "${ACTIVATION_SOURCES[@]}" "$TASK_ROOT/native/ProxiMicInput/Tests/Activation/main.swift" \
  -o "$BUILD_ROOT/activation-tests"
"$BUILD_ROOT/activation-tests"
xcrun swiftc -swift-version 5 -module-cache-path "$BUILD_ROOT/module-cache" \
  "$TASK_ROOT/native/ProxiMicInput/Sources/IPCClient.swift" \
  "$TASK_ROOT/native/ProxiMicInput/Tests/IPC/main.swift" -o "$BUILD_ROOT/ipc-harness"
"$TASK_ROOT/.runtime/venv/bin/python" -B "$TASK_ROOT/scripts/test-ime-ipc.py" "$BUILD_ROOT/ipc-harness"
