import QtQuick
import QtQuick.Controls

TextField {
    id: field
    required property var service
    required property string sequence
    readonly property bool shortcutRecorder: true
    signal recorded(string shortcut)
    readOnly: true
    selectByMouse: false
    text: activeFocus ? "" : sequence
    placeholderText: activeFocus ? "请按快捷键 · Esc 取消" : "点击后按快捷键"
    Accessible.description: "点击后直接按组合键，Esc 取消录制"
    onActiveFocusChanged: service.recording = activeFocus && visible
    onVisibleChanged: {
        if (!visible && activeFocus) {
            focus = false
            service.recording = false
        }
    }
    Keys.onShortcutOverride: function(event) { event.accepted = true }
    Connections {
        target: field.service
        function onShortcutCaptured(value) {
            if (field.activeFocus && field.visible) {
                // Release focus before saving; saving rebuilds the binding row.
                field.focus = false
                field.service.recording = false
                field.recorded(value)
            }
        }
    }
    Connections {
        target: Qt.application
        function onStateChanged() {
            if (Qt.application.state !== Qt.ApplicationActive && field.activeFocus) {
                field.focus = false
                field.service.recording = false
            }
        }
    }
    Component.onDestruction: { if (activeFocus && service) service.recording = false }
}
