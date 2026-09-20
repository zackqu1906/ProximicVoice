import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: settings
    required property var service
    readonly property string selectedApp: appPicker.currentValue || "codex"
    function actionLabel(action, fallback) {
        if (action === "previous") return selectedApp === "wechat" ? "上一个聊天" : "上一个任务"
        if (action === "next") return selectedApp === "wechat" ? "下一个聊天" : "下一个任务"
        return fallback
    }
    spacing: 10
    objectName: "appGestureSettingsSection"
    signal wechatSetupRequested()
    onSelectedAppChanged: service.recording = false
    onVisibleChanged: { if (!visible) service.recording = false }

    Label {
        Layout.fillWidth: true
        text: "根据前台应用自动切换。连接设备后即可使用，暂停语音或切回拼音也有效。"
        color: "#8D98AA"; wrapMode: Text.Wrap; font.pixelSize: 12
    }
    ComboBox {
        id: appPicker
        objectName: "appGestureAppPicker"
        Layout.fillWidth: true
        model: settings.service.apps
        textRole: "label"; valueRole: "value"
        Accessible.name: "选择要控制的应用"
        onActivated: settings.service.recording = false
    }
    Label {
        Layout.fillWidth: true; wrapMode: Text.Wrap
        text: settings.service.recordingError || (settings.service.recording ? "正在录制：按下组合键，Esc 取消" : "点击快捷键框后，直接按下组合键即可保存。")
        color: settings.service.recordingError ? "#F0B85A" : "#8D98AA"; font.pixelSize: 12
    }
    Repeater {
        model: settings.service.profiles[settings.selectedApp] || []
        delegate: Rectangle {
            id: row
            required property var modelData
            Layout.fillWidth: true
            implicitHeight: fields.implicitHeight + 24
            color: "#151B27"; radius: 10
            function save(gesture, shortcut, enabled) {
                settings.service.setBinding(settings.selectedApp, modelData.action, gesture, shortcut, enabled)
            }
            ColumnLayout {
                id: fields
                anchors.fill: parent; anchors.margins: 12
                spacing: 5
                RowLayout {
                    Layout.fillWidth: true
                    Label { Layout.fillWidth: true; text: settings.actionLabel(row.modelData.action, row.modelData.label); color: "#F5F7FB" }
                    Switch {
                        objectName: "appGestureEnabled_" + row.modelData.action
                        Layout.preferredHeight: 32
                        checked: row.modelData.enabled
                        Accessible.name: "启用" + row.modelData.label
                        onClicked: row.save(row.modelData.gesture, row.modelData.shortcut, checked)
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    ComboBox {
                        id: gesture
                        Layout.fillWidth: true; Layout.minimumWidth: 120
                        Layout.preferredHeight: 44
                        model: settings.service.gestures
                        textRole: "label"; valueRole: "value"
                        currentIndex: {
                            for (var i = 0; i < model.length; i++)
                                if (model[i].value === row.modelData.gesture) return i
                            return 0
                        }
                        Accessible.name: row.modelData.label + "手势"
                        onActivated: row.save(currentValue, row.modelData.shortcut, row.modelData.enabled)
                    }
                    ShortcutField {
                        objectName: "appGestureShortcut_" + row.modelData.action
                        Layout.fillWidth: true; Layout.minimumWidth: 200
                        Layout.preferredHeight: 44
                        sequence: row.modelData.shortcut
                        service: settings.service
                        Accessible.name: row.modelData.label + "快捷键"
                        onRecorded: function(value) { row.save(row.modelData.gesture, value, row.modelData.enabled) }
                    }
                }
            }
        }
    }
    Label {
        Layout.fillWidth: true; wrapMode: Text.Wrap
        text: "发送：听写中先定稿再发送。切换对话：本句处理完成后生效。\n手势可以复用；有可撤销或转编辑的语音时，优先处理语音操作。"
        color: "#8D98AA"; font.pixelSize: 11
    }
    Button {
        objectName: "openWeChatGuideButton"
        Layout.fillWidth: true
        visible: settings.selectedApp === "wechat"
        text: "微信聊天切换 · 查看设置步骤  ›"
        onClicked: settings.wechatSetupRequested()
    }
    Label {
        Layout.fillWidth: true; wrapMode: Text.Wrap
        text: settings.service.error
        visible: text.length > 0; color: "#F0B85A"; font.pixelSize: 12
    }
    Button {
        objectName: "resetAppGesturesButton"
        text: "恢复此应用默认手势"
        onClicked: settings.service.resetApp(settings.selectedApp)
    }
    Component.onDestruction: { if (settings.service) settings.service.recording = false }
}
