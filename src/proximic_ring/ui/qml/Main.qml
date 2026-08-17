import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: root
    width: 1080
    height: 820
    minimumWidth: 940
    minimumHeight: 700
    visible: true
    title: "ProxiMic Voice"
    color: "#0A0D12"
    Material.theme: Material.Dark
    Material.accent: "#7892FF"
    font.family: "Microsoft YaHei UI"

    property color panel: "#111620"
    property color panelAlt: "#151B27"
    property color border: "#232B3A"
    property color primary: "#7892FF"
    property color textMain: "#F5F7FB"
    property color textMuted: "#8D98AA"

    function statusColor() {
        if (appController.statusKind === "error") return "#FF6B7A"
        if (appController.statusKind === "manual") return "#C084FC"
        if (appController.statusKind === "listening") return "#4DD4AC"
        if (appController.statusKind === "running") return "#4DD4AC"
        if (appController.statusKind === "paused") return "#7892FF"
        if (appController.statusKind === "starting" || appController.statusKind === "stopping") return "#F5B942"
        return "#718096"
    }

    onClosing: function(close) {
        close.accepted = false
        root.hide()
        appController.requestQuit()
    }

    header: Rectangle {
        height: 78
        color: "#0D1118"
        border.color: root.border
        border.width: 1

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 30
            anchors.rightMargin: 30
            spacing: 14

            Rectangle {
                width: 42
                height: 42
                radius: 13
                gradient: Gradient {
                    GradientStop { position: 0; color: "#8AA4FF" }
                    GradientStop { position: 1; color: "#596EF2" }
                }
                Label {
                    anchors.centerIn: parent
                    text: "P"
                    color: "white"
                    font.pixelSize: 22
                    font.bold: true
                }
            }

            ColumnLayout {
                spacing: 0
                Label { text: "ProxiMic Voice"; color: root.textMain; font.pixelSize: 18; font.bold: true }
                Label { text: "近场智能语音输入"; color: root.textMuted; font.pixelSize: 12 }
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                implicitWidth: statusRow.implicitWidth + 26
                implicitHeight: 36
                radius: 18
                color: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.12)
                border.color: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.35)
                RowLayout {
                    id: statusRow
                    anchors.centerIn: parent
                    spacing: 8
                    Rectangle { width: 8; height: 8; radius: 4; color: root.statusColor() }
                    Label { text: appController.statusTitle; color: root.textMain; font.pixelSize: 13; font.bold: true }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 20

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 500
            spacing: 18

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 282
                radius: 22
                color: root.panel
                border.color: root.border

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 9

                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "语音输入"; color: root.textMain; font.pixelSize: 17; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label { text: "Ctrl + Alt + Space"; color: root.textMuted; font.pixelSize: 12 }
                    }

                    Item { Layout.fillHeight: true }

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        width: 112
                        height: 112
                        radius: 56
                        color: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.10)
                        border.color: root.statusColor()
                        border.width: 2
                        Rectangle {
                            anchors.centerIn: parent
                            width: 78
                            height: 78
                            radius: 39
                            color: Qt.rgba(root.statusColor().r, root.statusColor().g, root.statusColor().b, 0.20)
                            Label {
                                anchors.centerIn: parent
                                text: appController.recognitionEnabled ? "●" : "○"
                                color: root.statusColor()
                                font.pixelSize: 42
                            }
                        }
                    }

                    Label {
                        Layout.alignment: Qt.AlignHCenter
                        text: appController.statusTitle
                        color: root.textMain
                        font.pixelSize: 22
                        font.bold: true
                    }
                    Label {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.maximumWidth: 430
                        text: appController.statusDetail
                        color: root.textMuted
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 10

                        Button {
                            Layout.preferredWidth: appController.connected ? 170 : 220
                            Layout.preferredHeight: 44
                            enabled: !appController.busy
                            text: appController.connected
                                  ? (appController.recognitionEnabled ? "暂停语音识别" : "开启语音识别")
                                  : "连接设备"
                            font.pixelSize: 14
                            font.bold: true
                            onClicked: appController.connected
                                       ? appController.toggleRecognition()
                                       : appController.connectDevice()
                            contentItem: Label {
                                text: parent.text
                                color: "white"
                                font: parent.font
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: 12
                                color: parent.enabled ? (parent.down ? "#5B70E8" : root.primary) : "#3A4050"
                            }
                        }

                        Button {
                            visible: appController.connected
                            Layout.preferredWidth: 142
                            Layout.minimumWidth: 142
                            Layout.preferredHeight: 44
                            enabled: !appController.busy
                            text: "断开设备"
                            font.pixelSize: 14
                            onClicked: appController.disconnectDevice()
                            contentItem: Label {
                                text: parent.text
                                color: parent.enabled ? root.textMain : root.textMuted
                                font: parent.font
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                elide: Text.ElideNone
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 208
                radius: 18
                color: root.panel
                border.color: root.border
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "识别文本"; color: root.textMain; font.pixelSize: 15; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: !appController.transcriptFinal && appController.transcriptText.length > 0
                                  ? "实时：" + appController.transcriptText : ""
                            color: root.primary
                            visible: text.length > 0
                            font.pixelSize: 12
                            elide: Text.ElideRight
                            Layout.maximumWidth: 300
                        }
                        ToolButton { text: "清空"; onClicked: appController.clearEditor() }
                    }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        TextArea {
                            id: editorArea
                            text: appController.editorText
                            selectByMouse: true
                            color: root.textMain
                            font.pixelSize: 15
                            wrapMode: TextEdit.Wrap
                            leftPadding: 16
                            rightPadding: 16
                            topPadding: 14
                            bottomPadding: 14
                            background: Rectangle {
                                color: root.panelAlt
                                radius: 10
                                border.color: root.border

                                Label {
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.leftMargin: editorArea.leftPadding
                                    anchors.rightMargin: editorArea.rightPadding
                                    anchors.topMargin: editorArea.topPadding
                                    text: "最终识别结果会累积在这里，可选择、复制和直接编辑。"
                                    color: root.textMuted
                                    opacity: 0.65
                                    font: editorArea.font
                                    wrapMode: Text.Wrap
                                    visible: editorArea.text.length === 0
                                }
                            }
                            onTextChanged: {
                                if (activeFocus && text !== appController.editorText)
                                    appController.editorText = text
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 18
                color: root.panel
                border.color: root.border
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 18
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "运行日志"; color: root.textMain; font.pixelSize: 14; font.bold: true }
                        Item { Layout.fillWidth: true }
                        ToolButton { text: "清空"; onClicked: appController.clearLog() }
                    }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        TextArea {
                            readOnly: true
                            text: appController.logText.length > 0 ? appController.logText : "尚未启动"
                            color: root.textMuted
                            font.family: "Cascadia Mono"
                            font.pixelSize: 11
                            wrapMode: TextEdit.Wrap
                            background: Rectangle { color: "transparent" }
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.preferredWidth: 390
            Layout.fillHeight: true
            radius: 22
            color: root.panel
            border.color: root.border

            ScrollView {
                anchors.fill: parent
                anchors.margins: 2
                clip: true
                ColumnLayout {
                    width: 360
                    spacing: 14
                    enabled: !appController.connected && !appController.busy
                    opacity: enabled ? 1.0 : 0.55

                    Item { Layout.preferredHeight: 16 }
                    Label { text: "设备与识别设置"; color: root.textMain; font.pixelSize: 17; font.bold: true; Layout.leftMargin: 20 }
                    Label { text: "设置会自动保存，下次启动继续使用"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }

                    Label { text: "Ring 设备名称"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.deviceName
                        onEditingFinished: appController.deviceName = text
                    }
                    Label { text: "设备选择器（可选）"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.selector
                        placeholderText: "索引、名称或 BLE 地址"
                        onEditingFinished: appController.selector = text
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Label { text: "ProxiMic 模型"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.modelPath
                        onEditingFinished: appController.modelPath = text
                        onActiveFocusChanged: if (!activeFocus) cursorPosition = 0
                        Component.onCompleted: cursorPosition = 0
                    }
                    Label { text: "Stage1 threshold"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.stage1Threshold.toString()
                        validator: DoubleValidator { bottom: 0.000001; top: 1.0; notation: DoubleValidator.StandardNotation }
                        onEditingFinished: appController.stage1Threshold = Number(text)
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Label { text: "ASR 后端"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    ComboBox {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        model: ["streaming_sensevoice", "funasr_nano", "volcengine"]
                        currentIndex: Math.max(0, model.indexOf(appController.asrBackend))
                        onActivated: appController.asrBackend = currentText
                    }
                    Label { text: "ASR 模型"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.asrModel
                        placeholderText: appController.asrBackend === "funasr_nano" ? "留空则优先使用本地 checkpoint" : ""
                        onEditingFinished: appController.asrModel = text
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; spacing: 10
                        ColumnLayout {
                            Layout.fillWidth: true
                            Label { text: "运行设备"; color: root.textMuted; font.pixelSize: 12 }
                            TextField { Layout.fillWidth: true; text: appController.asrDevice; onEditingFinished: appController.asrDevice = text }
                        }
                        ColumnLayout {
                            Layout.preferredWidth: 110
                            Label { text: "语言"; color: root.textMuted; font.pixelSize: 12 }
                            ComboBox {
                                Layout.fillWidth: true
                                model: ["zh", "auto", "en", "yue", "ja", "ko"]
                                currentIndex: Math.max(0, model.indexOf(appController.asrLanguage))
                                onActivated: appController.asrLanguage = currentText
                            }
                        }
                    }
                    Label {
                        text: "streaming-sensevoice 目录"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.asrBackend === "streaming_sensevoice"
                    }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.streamingRepo
                        placeholderText: "留空则使用已安装的包"
                        onEditingFinished: appController.streamingRepo = text
                        onActiveFocusChanged: if (!activeFocus) cursorPosition = 0
                        Component.onCompleted: cursorPosition = 0
                        visible: appController.asrBackend === "streaming_sensevoice"
                    }
                    Label {
                        text: "Fun-ASR-main 目录"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.asrBackend === "funasr_nano"
                    }
                    TextField {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.funasrRepo
                        placeholderText: "必须包含 model.py"
                        onEditingFinished: appController.funasrRepo = text
                        onActiveFocusChanged: if (!activeFocus) cursorPosition = 0
                        Component.onCompleted: cursorPosition = 0
                        visible: appController.asrBackend === "funasr_nano"
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Switch {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "识别完成后输入到当前光标"
                        checked: appController.desktopOutputEnabled
                        onToggled: appController.desktopOutputEnabled = checked
                        visible: Qt.platform.os === "windows"
                    }
                    Switch {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "启用 Ctrl+Alt+Space 按住说话"
                        checked: appController.pushToTalkEnabled
                        onToggled: appController.pushToTalkEnabled = checked
                        visible: Qt.platform.os === "windows"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "macOS 当前支持 Ring、ProxiMic 和语音识别；全局按键与跨应用文字注入仅支持 Windows。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                        visible: Qt.platform.os !== "windows"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: Qt.platform.os === "windows"
                            ? "设备连接和语音识别相互独立；暂停识别不会断开 Ring。识别开启时，按键优先于自动靠近检测。"
                            : "设备连接和语音识别相互独立；暂停识别不会断开 Ring。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }
                    Item { Layout.preferredHeight: 22 }
                }
            }
        }
    }

    Window {
        id: transcriptOverlay
        width: Math.min(820, Math.max(360, overlayText.implicitWidth + 58))
        height: Math.max(74, overlayText.implicitHeight + 30)
        x: Math.round((Screen.width - width) / 2)
        y: Screen.height - height - 96
        visible: appController.transcriptVisible
        color: "transparent"
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus

        Rectangle {
            anchors.fill: parent
            radius: 18
            color: "#E9111620"
            border.color: appController.transcriptFinal ? "#594DD4AC" : "#477892FF"
            border.width: 1
            Label {
                id: overlayText
                anchors.fill: parent
                anchors.margins: 18
                text: appController.transcriptText
                color: appController.transcriptFinal ? "#8BE2C5" : "#F5F7FB"
                font.family: "Microsoft YaHei UI"
                font.pixelSize: 17
                wrapMode: Text.Wrap
                verticalAlignment: Text.AlignVCenter
            }
        }
    }
}
