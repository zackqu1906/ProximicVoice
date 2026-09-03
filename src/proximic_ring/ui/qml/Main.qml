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
    readonly property string uiFontFamily: Qt.platform.os === "osx"
                                           ? ".AppleSystemUIFont"
                                           : "Microsoft YaHei UI"
    font.family: uiFontFamily

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

    Connections {
        target: appController
        function onDevicePickerRequested() {
            devicePicker.open()
        }
    }

    Dialog {
        id: devicePicker
        objectName: "devicePicker"
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2)
        y: Math.round((parent.height - height) / 2)
        width: Math.min(620, parent.width - 48)
        height: Math.min(520, parent.height - 48)
        modal: true
        popupType: Popup.Item
        title: "选择蓝牙设备"
        closePolicy: Popup.CloseOnEscape
        onClosed: appController.stopDeviceDiscovery()

        contentItem: ColumnLayout {
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                TextField {
                    id: deviceSearchField
                    objectName: "deviceSearchField"
                    Layout.fillWidth: true
                    text: appController.deviceSearch
                    placeholderText: "搜索设备名称或标识"
                    selectByMouse: true
                    onTextEdited: appController.deviceSearch = text
                }

                Button {
                    text: "清除"
                    enabled: deviceSearchField.text.length > 0
                    onClicked: {
                        deviceSearchField.clear()
                        appController.deviceSearch = ""
                        deviceSearchField.forceActiveFocus()
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: appController.scanMessage
                    color: root.textMuted
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                }
                BusyIndicator {
                    running: appController.scanBusy
                    visible: running
                    implicitWidth: 30
                    implicitHeight: 30
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 12
                color: root.panelAlt
                border.color: root.border
                clip: true

                ListView {
                    id: deviceList
                    objectName: "deviceList"
                    anchors.fill: parent
                    anchors.margins: 6
                    spacing: 6
                    clip: true
                    model: appController.availableDevices
                    ScrollBar.vertical: ScrollBar { }

                    delegate: Rectangle {
                        required property var modelData
                        width: deviceList.width
                        height: 70
                        radius: 9
                        color: connectButton.hovered ? "#20293A" : "transparent"
                        border.color: connectButton.hovered ? root.primary : "transparent"

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 10
                            spacing: 12

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 3
                                Label {
                                    Layout.fillWidth: true
                                    text: modelData.name
                                    color: root.textMain
                                    font.pixelSize: 14
                                    font.bold: true
                                    elide: Text.ElideRight
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: modelData.identifier
                                          + (modelData.rssi === "" ? "" : "  ·  " + modelData.rssi + " dBm")
                                    color: root.textMuted
                                    font.pixelSize: 11
                                    elide: Text.ElideMiddle
                                }
                            }

                            Button {
                                id: connectButton
                                text: "连接"
                                enabled: !appController.busy
                                onClicked: {
                                    devicePicker.close()
                                    appController.connectToDevice(modelData.identifier, modelData.name)
                                }
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: appController.scanBusy ? "扫描中…" : "重新扫描"
                    enabled: !appController.scanBusy && !appController.busy
                    onClicked: appController.scanDevices()
                }
                Button {
                    text: "取消"
                    onClicked: devicePicker.close()
                }
            }
        }
    }

    Dialog {
        id: gpuInstallDialog
        objectName: "gpuInstallDialog"
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2)
        y: Math.round((parent.height - height) / 2)
        width: Math.min(500, parent.width - 48)
        implicitHeight: 210
        modal: true
        popupType: Popup.Item
        title: "安装 NVIDIA GPU 加速"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: appController.installGpuSupport()

        contentItem: Label {
            text: "安装需要下载数 GB 文件。应用将退出并打开独立安装窗口；安装验证成功后会自动重新启动。是否继续？"
            color: root.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
    }

    Dialog {
        id: runtimeLogDialog
        objectName: "runtimeLogDialog"
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2)
        y: Math.round((parent.height - height) / 2)
        width: Math.min(820, parent.width - 48)
        height: Math.min(620, parent.height - 48)
        modal: true
        popupType: Popup.Item
        title: "完整实时日志"
        closePolicy: Popup.CloseOnEscape

        contentItem: ColumnLayout {
            spacing: 12

            ScrollView {
                id: logScroll
                objectName: "logScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true

                TextArea {
                    id: logArea
                    objectName: "logArea"
                    width: logScroll.availableWidth
                    readOnly: true
                    textFormat: TextEdit.PlainText
                    text: "尚未启动"
                    color: root.textMuted
                    font.family: Qt.platform.os === "osx" ? "Menlo" : "Cascadia Mono"
                    font.pixelSize: 14
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    background: Rectangle { color: root.panelAlt; radius: 10 }

                    function refreshLog() {
                        var nextText = appController.logText
                        text = nextText.length > 0 ? nextText : "尚未启动"
                        cursorPosition = length
                    }

                    Component.onCompleted: refreshLog()
                    Connections {
                        target: appController
                        function onLogChanged() { logArea.refreshLog() }
                    }
                    onTextChanged: cursorPosition = length
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "日志会在窗口打开期间继续实时更新"
                    color: root.textMuted
                    font.pixelSize: 11
                }
                Button { text: "清空"; onClicked: appController.clearLog() }
                Button { text: "关闭"; onClicked: runtimeLogDialog.close() }
            }
        }
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

            Button {
                id: runtimeLogButton
                objectName: "runtimeLogButton"
                text: "实时日志"
                onClicked: runtimeLogDialog.open()
            }

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
                id: voiceInputCard
                objectName: "voiceInputCard"
                Layout.fillWidth: true
                Layout.preferredHeight: (appController.reviewPending ? 420 : 380)
                                        + (appController.macOSAccessibilityRequired ? 102 : 0)
                radius: 22
                color: root.panel
                border.color: root.border

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 9

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 92
                        visible: appController.macOSAccessibilityRequired
                        radius: 12
                        color: "#4A372A"
                        border.width: 1
                        border.color: "#D89B57"

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 12
                            Label {
                                Layout.fillWidth: true
                                text: "macOS 尚未允许当前安装包跨应用输入；浮窗可显示，但听写和编辑不会注入。\n授权后会自动检测，无需重启。如这里已显示开启，请删除旧条目，再重新添加 /Applications/Proximic Voice.app。"
                                color: "#FFE1BD"
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                            }
                            Button {
                                text: "打开辅助功能设置"
                                onClicked: appController.openMacOSAccessibilitySettings()
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "全局语音输入"; color: root.textMain; font.pixelSize: 17; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: Qt.platform.os === "windows"
                                  ? (appController.inputRoutingMode === "auto"
                                     ? "自动判断听写/指令 / 右 Alt 说话"
                                     : "Alt+1 输入 / Alt+2 修改 / 右 Alt 说话")
                                  : "macOS 输入 / 编辑"
                            color: root.textMuted
                            font.pixelSize: 12
                        }
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 10

                        Button {
                            id: dictationModeButton
                            objectName: "dictationModeButton"
                            Layout.preferredWidth: 150
                            Layout.preferredHeight: 42
                            text: "输入到光标"
                            checkable: true
                            autoExclusive: true
                            visible: appController.inputRoutingMode === "manual"
                            enabled: appController.inputRoutingMode === "manual"
                            checked: appController.inputMode === "dictation"
                            onClicked: appController.inputMode = "dictation"
                            ToolTip.visible: hovered
                            ToolTip.text: appController.llmEnabled
                                ? "文本 LLM 整理后输入到说话开始时的外部文本框"
                                : "直接把 ASR 最终结果输入到说话开始时的外部文本框"
                        }
                        Button {
                            id: editModeButton
                            objectName: "editModeButton"
                            Layout.preferredWidth: 150
                            Layout.preferredHeight: 42
                            text: "修改当前文本"
                            checkable: true
                            autoExclusive: true
                            visible: appController.inputRoutingMode === "manual"
                            enabled: appController.inputRoutingMode === "manual"
                                     && !appController.reviewPending
                            checked: appController.inputMode === "edit"
                            onClicked: appController.inputMode = "edit"
                            ToolTip.visible: hovered
                            ToolTip.text: "读取当前外部文本框，下一段语音作为增删改指令"
                        }
                        Button {
                            id: dictationLlmButton
                            objectName: "dictationLlmButton"
                            Layout.preferredWidth: 130
                            Layout.preferredHeight: 42
                            text: appController.llmEnabled ? "LLM 整理：开" : "LLM 整理：关"
                            onClicked: appController.toggleDictationLlm()
                            ToolTip.visible: hovered
                            ToolTip.text: "仅影响听写后的二次整理；自动模式的听写/指令判断始终使用所选 LLM"
                            contentItem: Label {
                                text: parent.text
                                color: appController.llmEnabled ? "#DCE4FF" : root.textMain
                                font.pixelSize: 13
                                font.bold: true
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: 10
                                color: parent.down
                                       ? "#34415A"
                                       : (appController.llmEnabled ? "#314472" : root.panelAlt)
                                border.width: 1
                                border.color: appController.llmEnabled ? root.primary : root.border
                            }
                        }
                    }

                    Label {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.maximumWidth: 430
                        text: appController.inputRoutingMode === "auto"
                              ? "自动判断听写/指令；判断后可按 Tab 纠正，整句处理期间按 Esc 取消"
                              : (appController.inputMode === "edit"
                              ? "把光标留在目标文本框；修改预览 2 秒后自动应用，Enter 立即应用，Esc 取消"
                              : (appController.llmEnabled
                                 ? "下一段语音经文本 LLM 整理后，输入到当前外部文本框"
                                 : "下一段语音直接采用 ASR 最终结果，不再经过文本 LLM"))
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        visible: appController.reviewPending
                        spacing: 8

                        Button {
                            objectName: "confirmEditButton"
                            text: appController.editAutoConfirmText === ""
                                  ? "立即应用"
                                  : "立即应用 · " + appController.editAutoConfirmText
                            visible: appController.reviewCanConfirm
                            onClicked: appController.dispatchVoiceAction("confirm")
                        }
                        Button {
                            objectName: "cancelEditButton"
                            text: "取消"
                            onClicked: appController.dispatchVoiceAction("cancel")
                        }
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
                        Layout.fillWidth: true
                        Layout.maximumWidth: 430
                        Layout.maximumHeight: 38
                        text: appController.statusDetail
                              + (appController.textProcessing ? " · 大模型处理中" : "")
                        color: root.textMuted
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.minimumHeight: 44
                        Layout.bottomMargin: 2
                        spacing: 10

                        Button {
                            objectName: "primaryConnectionButton"
                            Layout.preferredWidth: appController.connected
                                                   ? 170
                                                   : (appController.canReconnect ? 190 : 220)
                            Layout.preferredHeight: 44
                            enabled: !appController.busy && !appController.scanBusy
                            text: appController.connected
                                  ? (appController.recognitionEnabled ? "暂停语音识别" : "开启语音识别")
                                  : (appController.scanBusy
                                     ? "正在扫描设备…"
                                     : (appController.canReconnect ? "重新连接设备" : "选择并连接设备"))
                            font.pixelSize: 14
                            font.bold: true
                            onClicked: {
                                if (appController.connected)
                                    appController.toggleRecognition()
                                else if (appController.canReconnect)
                                    appController.reconnectDevice()
                                else
                                    appController.requestDevicePicker()
                            }
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
                            objectName: "secondaryConnectionButton"
                            visible: appController.connected || appController.canReconnect
                            Layout.preferredWidth: 142
                            Layout.minimumWidth: 142
                            Layout.preferredHeight: 44
                            enabled: appController.connected
                                     ? appController.statusKind !== "stopping"
                                     : (!appController.busy && !appController.scanBusy)
                            text: appController.connected ? "断开设备" : "选择其他设备"
                            font.pixelSize: 14
                            onClicked: appController.connected
                                       ? appController.disconnectDevice()
                                       : appController.requestDevicePicker()
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
                id: voiceHistoryCard
                objectName: "voiceHistoryCard"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 250
                radius: 18
                color: root.panel
                border.color: root.border
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "逐句语音记录"; color: root.textMain; font.pixelSize: 15; font.bold: true }
                        Item { Layout.fillWidth: true }
                        ToolButton { text: "清空"; onClicked: appController.clearVoiceHistory() }
                    }

                    ListView {
                        id: voiceHistoryList
                        objectName: "voiceHistoryList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 8
                        model: appController.voiceHistoryEntries
                        ScrollBar.vertical: ScrollBar { }
                        delegate: Rectangle {
                            required property var modelData
                            width: voiceHistoryList.width
                            height: Math.max(68, historyContent.implicitHeight + 18)
                            radius: 10
                            color: root.panelAlt
                            border.color: root.border

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 12
                                anchors.rightMargin: 10
                                anchors.topMargin: 8
                                anchors.bottomMargin: 8
                                spacing: 10

                                ColumnLayout {
                                    id: historyContent
                                    Layout.fillWidth: true
                                    spacing: 4
                                    Label {
                                        text: modelData.displayTime + "  ·  "
                                              + modelData.durationLabel
                                              + (modelData.backend ? "  ·  " + modelData.backend : "")
                                        color: root.textMuted
                                        font.pixelSize: 10
                                    }
                                    Label {
                                        id: historyText
                                        Layout.fillWidth: true
                                        text: modelData.text
                                        color: modelData.recognized ? root.textMain : root.textMuted
                                        font.pixelSize: 13
                                        wrapMode: Text.Wrap
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        visible: Boolean(modelData.candidateText)
                                                 && modelData.candidateText !== modelData.text
                                        text: "处理结果：" + modelData.candidateText
                                        color: "#C9B2F2"
                                        font.pixelSize: 12
                                        wrapMode: Text.Wrap
                                    }
                                }
                                Button {
                                    objectName: "voiceHistoryOpenLocationButton"
                                    Layout.minimumWidth: 76
                                    Layout.preferredWidth: 76
                                    text: "打开位置"
                                    font.pixelSize: 12
                                    onClicked: appController.openVoiceHistoryLocation(modelData.audioPath)
                                }
                                Button {
                                    objectName: "voiceHistoryPlayButton"
                                    Layout.minimumWidth: 88
                                    Layout.preferredWidth: 88
                                    text: appController.playingVoicePath === modelData.audioPath
                                          ? "停止播放" : "播放录音"
                                    font.pixelSize: 12
                                    contentItem: Label {
                                        text: parent.text
                                        color: parent.enabled ? root.textMain : root.textMuted
                                        font: parent.font
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideNone
                                    }
                                    onClicked: appController.playVoiceHistory(modelData.audioPath)
                                }
                            }
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: voiceHistoryList.count === 0
                            text: "每段语音结束后，会在这里保存录音和识别文字"
                            color: root.textMuted
                            font.pixelSize: 12
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Switch {
                            objectName: "smartAssociationSwitch"
                            Layout.fillWidth: true
                            text: "智能关联推荐"
                            checked: appController.smartAssociationEnabled
                            onToggled: appController.smartAssociationEnabled = checked
                        }
                        Button {
                            objectName: "openAssociationCenterButton"
                            Layout.minimumWidth: 132
                            text: "数据关联中心"
                            onClicked: appController.performAssociationAction(
                                "center.open", ""
                            )
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

                    Label { text: "Ring 设备"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "通过主界面的“选择并连接设备”扫描附近设备，再点击对应设备连接。"
                        color: root.textMain
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    Label { text: "Ring 音频编码"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    ComboBox {
                        id: audioEncodingCombo
                        objectName: "audioEncodingCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        model: ["PCM（原始，高带宽）", "ADPCM（低带宽）", "Opus（推荐，连接更稳定）"]
                        currentIndex: Math.max(0, ["pcm", "adpcm", "opus"].indexOf(appController.audioEncoding))
                        onActivated: appController.audioEncoding = ["pcm", "adpcm", "opus"][currentIndex]
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.audioEncoding === "pcm"
                              ? "原始 PCM 带宽最高，BLE 链路繁忙时更容易出现音频停流。"
                              : appController.audioEncoding === "adpcm"
                                ? "BLE 带宽较低，但有损压缩可能改变近点模型的 Stage2 分数分布。"
                                : "默认使用 Opus 降低 BLE 带宽；SDK 解码后仍向模型提供 16 kHz PCM。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
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
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Label {
                            text: "ASR 输入增益"
                            color: root.textMuted
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: "+" + appController.asrGainDb.toFixed(0) + " dB"
                            color: root.textMain
                            font.pixelSize: 12
                            font.bold: true
                        }
                    }
                    Slider {
                        id: asrGainSlider
                        objectName: "asrGainSlider"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        from: 0
                        to: 12
                        stepSize: 1
                        value: appController.asrGainDb
                        onMoved: appController.asrGainDb = value
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "仅增强送入 ASR 和语音记录的音频，不影响近点模型。默认 0 dB；弱声可先试 +6 dB，过高可能削波，重新连接后生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; spacing: 10
                        ColumnLayout {
                            Layout.fillWidth: true
                            Label { text: "运行设备（本地 ASR）"; color: root.textMuted; font.pixelSize: 12 }
                            ComboBox {
                                id: asrDeviceCombo
                                objectName: "asrDeviceCombo"
                                Layout.fillWidth: true
                                model: appController.computeDevices
                                textRole: "label"
                                valueRole: "value"
                                enabled: appController.asrBackend !== "volcengine"
                                currentIndex: Math.max(0, indexOfValue(appController.asrDevice))
                                onActivated: appController.asrDevice = currentValue
                            }
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
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: appController.asrBackend === "volcengine"
                              ? "火山引擎是云端识别，不使用本机 CPU 或 GPU；Key 会保存在当前用户的应用设置中。"
                              : appController.gpuStatusText
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    Label {
                        text: "线上语音模型 API Key"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.asrBackend === "volcengine"
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        spacing: 8
                        visible: appController.asrBackend === "volcengine"

                        TextField {
                            id: asrApiKeyField
                            objectName: "asrApiKeyField"
                            Layout.fillWidth: true
                            text: appController.asrApiKey
                            placeholderText: "填写豆包语音 App Key"
                            echoMode: showAsrApiKeyButton.checked
                                      ? TextInput.Normal : TextInput.Password
                            onEditingFinished: appController.asrApiKey = text
                        }
                        ToolButton {
                            id: showAsrApiKeyButton
                            objectName: "showAsrApiKeyButton"
                            checkable: true
                            text: checked ? "隐藏" : "显示"
                        }
                    }
                    Button {
                        id: gpuInstallButton
                        objectName: "gpuInstallButton"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "安装 NVIDIA GPU 加速"
                        visible: appController.gpuInstallerAvailable
                                 && appController.asrBackend !== "volcengine"
                        enabled: !appController.connected && !appController.busy
                        onClicked: gpuInstallDialog.open()
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
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        visible: appController.asrBackend === "funasr_nano"
                        spacing: 8

                        Label {
                            text: "识别热词"
                            color: root.textMain
                            font.pixelSize: 12
                            font.bold: true
                        }
                        Label {
                            text: "每行一个"
                            color: root.textMuted
                            font.pixelSize: 11
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Layout.preferredHeight: 116
                        visible: appController.asrBackend === "funasr_nano"
                        color: root.panelAlt
                        radius: 9
                        border.width: 1
                        border.color: asrHotwordsField.activeFocus
                                      ? root.primary : root.border
                        clip: true

                        ScrollView {
                            id: asrHotwordsScroll
                            anchors.fill: parent
                            anchors.margins: 1
                            clip: true
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                            ScrollBar.vertical.policy: ScrollBar.AsNeeded

                            TextArea {
                                id: asrHotwordsField
                                objectName: "asrHotwordsField"
                                width: asrHotwordsScroll.availableWidth
                                text: appController.asrHotwords
                                color: root.textMain
                                font.pixelSize: 14
                                wrapMode: TextEdit.Wrap
                                selectByMouse: true
                                leftPadding: 12
                                rightPadding: 12
                                topPadding: 10
                                bottomPadding: 10
                                background: Rectangle { color: "transparent" }
                                onActiveFocusChanged: {
                                    if (!activeFocus)
                                        appController.asrHotwords = text
                                }
                            }
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "也支持逗号或分号分隔；自动去空和去重，重新连接后生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        visible: appController.asrBackend === "funasr_nano"
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Label { text: "听写 / 指令切换"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    ComboBox {
                        id: inputRoutingModeCombo
                        objectName: "inputRoutingModeCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        model: ["自动判断（LLM）", "手动切换"]
                        currentIndex: appController.inputRoutingMode === "auto" ? 0 : 1
                        onActivated: appController.inputRoutingMode = currentIndex === 0 ? "auto" : "manual"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.inputRoutingMode === "auto"
                              ? "每段语音结束后先调用所选文本 LLM 判断听写或编辑指令；日志会记录开始时间、结束时间和判断耗时。"
                              : "沿用上方“输入到光标 / 修改当前文本”的固定模式；Alt+1、Alt+2 以及后续手势只负责手动切换。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }

                    Label { text: "文本大模型"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    ComboBox {
                        id: llmProviderCombo
                        objectName: "llmProviderCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        model: ["本地 GGUF", "火山方舟（在线）"]
                        currentIndex: appController.llmProvider === "local" ? 0 : 1
                        onActivated: appController.llmProvider = currentIndex === 0 ? "local" : "volcengine"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmProvider === "local"
                            ? "自动路由和修改模式使用本地 GGUF；听写是否二次整理由上方开关决定。首次处理时自动启动，全程离线。"
                            : "自动路由和修改模式使用所选在线模型；听写是否二次整理由上方开关决定。Key 会保存在当前用户的应用设置中。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        visible: appController.llmProvider === "local"
                        Label {
                            Layout.fillWidth: true
                            text: appController.localModelInstallStatus
                            color: appController.localModelInstalled ? "#4DD4AC" : root.textMuted
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                        Button {
                            objectName: "installLocalModelButton"
                            text: appController.localModelInstalled
                                ? "已安装"
                                : (appController.localModelInstalling ? "下载中…" : "下载本地模型")
                            enabled: !appController.localModelInstalled && !appController.localModelInstalling
                            onClicked: appController.installLocalModel()
                        }
                    }
                    Label {
                        text: "本地 llama-server.exe"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider === "local"
                    }
                    TextField {
                        id: llmLocalServerField
                        objectName: "llmLocalServerField"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmLocalServerPath
                        placeholderText: "llama-server.exe 的完整路径"
                        onEditingFinished: appController.llmLocalServerPath = text
                        onActiveFocusChanged: if (!activeFocus) cursorPosition = 0
                        Component.onCompleted: cursorPosition = 0
                        visible: appController.llmProvider === "local"
                    }
                    Label {
                        text: "本地 GGUF 模型"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider === "local"
                    }
                    TextField {
                        id: llmLocalModelField
                        objectName: "llmLocalModelField"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmLocalModelPath
                        placeholderText: "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf 的完整路径"
                        onEditingFinished: appController.llmLocalModelPath = text
                        onActiveFocusChanged: if (!activeFocus) cursorPosition = 0
                        Component.onCompleted: cursorPosition = 0
                        visible: appController.llmProvider === "local"
                    }
                    Label {
                        text: "方舟 API Base URL"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    TextField {
                        id: llmBaseUrlField
                        objectName: "llmBaseUrlField"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmBaseUrl
                        onEditingFinished: appController.llmBaseUrl = text
                        visible: appController.llmProvider !== "local"
                    }
                    Label {
                        text: "方舟模型"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    ComboBox {
                        id: llmModelCombo
                        objectName: "llmModelCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        model: ["豆包 Seed 2.0 Lite", "DeepSeek V4 Flash"]
                        currentIndex: appController.llmModel === "deepseek-v4-flash-260425" ? 1 : 0
                        onActivated: appController.llmModel = currentIndex === 0
                            ? "doubao-seed-2-0-lite-260215"
                            : "deepseek-v4-flash-260425"
                        visible: appController.llmProvider !== "local"
                    }
                    Label {
                        text: "模型 ID（高级配置）"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    TextField {
                        id: llmModelField
                        objectName: "llmModelField"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmModel
                        placeholderText: "方舟 Model ID"
                        onEditingFinished: appController.llmModel = text
                        visible: appController.llmProvider !== "local"
                    }
                    Label {
                        text: "线上大模型 API Key"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        spacing: 8
                        visible: appController.llmProvider !== "local"

                        TextField {
                            id: llmApiKeyField
                            objectName: "llmApiKeyField"
                            Layout.fillWidth: true
                            text: appController.llmApiKey
                            placeholderText: "填写火山方舟 API Key"
                            echoMode: showLlmApiKeyButton.checked
                                      ? TextInput.Normal : TextInput.Password
                            onEditingFinished: appController.llmApiKey = text
                        }
                        ToolButton {
                            id: showLlmApiKeyButton
                            objectName: "showLlmApiKeyButton"
                            checkable: true
                            text: checked ? "隐藏" : "显示"
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        Label { text: "请求超时（秒）"; color: root.textMuted; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                        SpinBox {
                            from: 1
                            to: 300
                            value: Math.round(appController.llmTimeoutSeconds)
                            onValueModified: appController.llmTimeoutSeconds = value
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Switch {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "识别完成后输入到当前光标"
                        checked: appController.desktopOutputEnabled
                        onToggled: appController.desktopOutputEnabled = checked
                        visible: Qt.platform.os === "windows" || Qt.platform.os === "osx"
                    }
                    Switch {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "启用右 Alt 按住说话"
                        checked: appController.pushToTalkEnabled
                        onToggled: appController.pushToTalkEnabled = checked
                        visible: Qt.platform.os === "windows"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "macOS 听写和编辑可作用于当前文本框；首次使用请在系统设置的“隐私与安全性 → 辅助功能”中允许 Proximic Voice。整句支持 Esc 取消，自动分类后 Tab 纠错，编辑预览 Enter 立即应用；右 Alt 控制仍仅支持 Windows。"
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
        objectName: "transcriptOverlay"
        readonly property bool showAutoModeBadge:
            appController.inputRoutingMode === "auto"
            && appController.transcriptMode !== ""
        readonly property real modeBadgeReserve: showAutoModeBadge ? 78 : 0
        width: Math.min(820, Math.max(360,
            Math.max(overlayText.implicitWidth + modeBadgeReserve,
                     editPreviewText.implicitWidth) + 58))
        height: Math.min(420, Math.max(74, overlayContent.implicitHeight + 36))
        x: Math.round((Screen.width - width) / 2)
        y: Screen.height - height - 96
        visible: appController.transcriptVisible
        color: "transparent"
        // Qt.Tool maps to an NSPanel that macOS hides when another app gets
        // focus. The transcript must remain visible over that target app.
        flags: (Qt.platform.os === "osx" ? Qt.Window : Qt.Tool)
               | Qt.FramelessWindowHint
               | Qt.WindowStaysOnTopHint
               | Qt.WindowDoesNotAcceptFocus

        Rectangle {
            anchors.fill: parent
            radius: 18
            color: "#E9111620"
            border.color: appController.reviewFailed
                          ? "#8AFF646F"
                          : appController.reviewPending
                            ? "#8AC084FC"
                            : (appController.transcriptFinal ? "#594DD4AC" : "#477892FF")
            border.width: 1
            Column {
                id: overlayContent
                anchors.centerIn: parent
                width: parent.width - 36
                spacing: (appController.reviewPending
                          || appController.interactionCanCancel
                          || appController.undoAvailable) ? 10 : 0

                RowLayout {
                    width: parent.width
                    spacing: transcriptOverlay.showAutoModeBadge ? 12 : 0

                    Label {
                        id: overlayText
                        Layout.fillWidth: true
                        text: appController.transcriptText
                        color: appController.reviewFailed
                               ? "#FF9DA5"
                               : appController.reviewPending
                                 ? "#E4D0FF"
                                 : (appController.transcriptFinal ? "#8BE2C5" : "#F5F7FB")
                        font.family: "Microsoft YaHei UI"
                        font.pixelSize: 17
                        wrapMode: Text.Wrap
                    }

                    Rectangle {
                        id: autoModeBadge
                        objectName: "autoModeBadge"
                        Layout.preferredWidth: 66
                        Layout.preferredHeight: 30
                        Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                        visible: transcriptOverlay.showAutoModeBadge
                        radius: 15
                        color: appController.transcriptMode === "edit"
                               ? "#553F2A68"
                               : "#4030705C"
                        border.width: 1
                        border.color: appController.transcriptMode === "edit"
                                      ? "#AFC084FC"
                                      : "#9A4DD4AC"

                        Label {
                            anchors.centerIn: parent
                            text: appController.transcriptMode === "edit" ? "指令" : "听写"
                            color: appController.transcriptMode === "edit"
                                   ? "#E4D0FF"
                                   : "#8BE2C5"
                            font.family: "Microsoft YaHei UI"
                            font.pixelSize: 13
                            font.bold: true
                        }
                    }
                }

                Label {
                    width: parent.width
                    visible: appController.reviewCanConfirm
                    text: "修改后："
                    color: "#AEB8C8"
                    font.family: root.uiFontFamily
                    font.pixelSize: 13
                }

                Label {
                    id: editPreviewText
                    objectName: "editPreviewText"
                    width: parent.width
                    visible: appController.reviewCanConfirm
                    text: appController.editPreviewHtml
                    textFormat: Text.RichText
                    color: "#F5F7FB"
                    font.family: root.uiFontFamily
                    font.pixelSize: 17
                    wrapMode: Text.Wrap
                }

                RowLayout {
                    width: parent.width
                    visible: appController.interactionCanCancel
                             || appController.undoAvailable
                    spacing: 8

                    Label {
                        Layout.fillWidth: true
                        visible: appController.reviewCanConfirm
                        text: appController.editAutoConfirmText
                        color: "#C9B2F2"
                        font.family: root.uiFontFamily
                        font.pixelSize: 12
                    }

                    Button {
                        objectName: "switchModeButton"
                        visible: appController.modeCorrectionAvailable
                        text: appController.modeCorrectionLabel + " · Tab"
                        onClicked: appController.dispatchVoiceAction("switch_mode")
                    }

                    Button {
                        objectName: "overlayConfirmButton"
                        visible: appController.reviewCanConfirm
                        text: "立即应用 · Enter"
                        onClicked: appController.dispatchVoiceAction("confirm")
                    }

                    Button {
                        objectName: "cancelUtteranceButton"
                        visible: appController.interactionCanCancel
                        text: "取消本句 · Esc"
                        onClicked: appController.dispatchVoiceAction("cancel")
                    }

                    Button {
                        objectName: "undoAppliedButton"
                        visible: appController.undoAvailable
                        text: "撤回上次结果 · "
                              + appController.undoRemainingSeconds + "s"
                        onClicked: appController.dispatchVoiceAction("undo")
                    }
                }

            }
        }
    }

    Window {
        id: associationRecommendationOverlay
        objectName: "associationRecommendationOverlay"
        readonly property bool hasTargetBounds:
            appController.associationPopupTargetWidth > 0
            && appController.associationPopupTargetHeight > 0
        width: 430
        height: 194
        x: {
            var preferred = Screen.width - width - 32
            if (hasTargetBounds) {
                var right = appController.associationPopupTargetX
                          + appController.associationPopupTargetWidth + 12
                var left = appController.associationPopupTargetX - width - 12
                preferred = right + width <= Screen.width - 12 ? right : left
            }
            return Math.round(Math.max(12, Math.min(Screen.width - width - 12,
                                                     preferred)))
        }
        y: {
            var preferred = hasTargetBounds
                    ? appController.associationPopupTargetY : 72
            var maximum = appController.transcriptVisible
                    ? transcriptOverlay.y - height - 12
                    : Screen.height - height - 20
            return Math.round(Math.max(20, Math.min(maximum, preferred)))
        }
        visible: appController.associationRecommendationVisible
                 && !appController.associationDetailVisible
                 && !appController.associationCenterVisible
        color: "transparent"
        flags: (Qt.platform.os === "osx" ? Qt.Window : Qt.Tool)
               | Qt.FramelessWindowHint
               | Qt.WindowStaysOnTopHint
        onClosing: function(close) {
            close.accepted = false
            appController.performAssociationAction("recommendation.reject", "")
        }

        Rectangle {
            anchors.fill: parent
            radius: 16
            color: "#F2141924"
            border.color: "#8A4DD4AC"
            border.width: 1

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 9

                Label {
                    Layout.fillWidth: true
                    text: appController.associationRecommendationTitle
                    color: root.textMain
                    font.pixelSize: 15
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    text: appController.associationRecommendationPositiveLabel
                          + "："
                          + appController.associationRecommendationPositiveText
                    color: "#8BE2C5"
                    font.pixelSize: 13
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                }
                Item { Layout.fillHeight: true }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Button {
                        objectName: "acceptAssociationRecommendationButton"
                        Layout.minimumWidth: 92
                        text: "关联"
                        onClicked: appController.performAssociationAction(
                            "recommendation.accept", ""
                        )
                    }
                    Button {
                        objectName: "showAssociationDetailsButton"
                        Layout.minimumWidth: 108
                        text: "查看详情"
                        onClicked: appController.performAssociationAction(
                            "recommendation.details.open", ""
                        )
                    }
                    Item { Layout.fillWidth: true }
                    Button {
                        objectName: "rejectAssociationRecommendationButton"
                        Layout.minimumWidth: 92
                        text: "不关联"
                        onClicked: appController.performAssociationAction(
                            "recommendation.reject", ""
                        )
                    }
                }
            }
        }
    }

    Window {
        id: associationDetailsWindow
        objectName: "associationDetailsWindow"
        width: 620
        height: 560
        minimumWidth: 540
        minimumHeight: 420
        x: Math.round((Screen.width - width) / 2)
        y: appController.transcriptVisible
           ? Math.round(Math.max(20, transcriptOverlay.y - height - 16))
           : Math.round((Screen.height - height) / 2)
        visible: appController.associationDetailVisible
        title: "推荐关联详情"
        color: root.color
        flags: Qt.Window | Qt.WindowStaysOnTopHint
        onClosing: function(close) {
            close.accepted = false
            appController.performAssociationAction(
                "recommendation.details.close", ""
            )
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            Label {
                text: "推荐关联详情"
                color: root.textMain
                font.pixelSize: 19
                font.bold: true
            }
            Label {
                text: appController.associationRecommendationTitle
                color: root.textMuted
                font.pixelSize: 12
            }
            ListView {
                id: associationDetailsList
                objectName: "associationDetailsList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 10
                clip: true
                model: appController.associationDetailEntries
                ScrollBar.vertical: ScrollBar { }
                delegate: Rectangle {
                    required property var modelData
                    width: associationDetailsList.width
                    height: detailColumn.implicitHeight + 24
                    radius: 12
                    color: modelData.role === "chosen" ? "#253E3540" : "#3A282D40"
                    border.width: 2
                    border.color: modelData.role === "chosen" ? "#4DD4AC" : "#FF646F"
                    ColumnLayout {
                        id: detailColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 12
                        spacing: 6
                        Label {
                            text: modelData.role === "chosen" ? "✓ 正例" : "× 反例"
                            color: modelData.role === "chosen" ? "#8BE2C5" : "#FF9DA5"
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "ASR：" + modelData.asrText
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: Boolean(modelData.resultText)
                            text: "结果：" + modelData.resultText
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                Layout.fillWidth: true
                                text: modelData.status
                                color: root.textMuted
                                font.pixelSize: 11
                            }
                            Button {
                                Layout.minimumWidth: 92
                                visible: Boolean(modelData.audioPath)
                                text: "播放录音"
                                onClicked: appController.playVoiceHistory(
                                    modelData.audioPath
                                )
                            }
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Button {
                    Layout.minimumWidth: 112
                    text: "关联"
                    onClicked: appController.performAssociationAction(
                        "recommendation.accept", ""
                    )
                }
                Item { Layout.fillWidth: true }
                Button {
                    Layout.minimumWidth: 112
                    text: "不关联"
                    onClicked: appController.performAssociationAction(
                        "recommendation.reject", ""
                    )
                }
            }
        }
    }

    Window {
        id: associationCenterWindow
        objectName: "associationCenterWindow"
        width: 760
        height: 680
        minimumWidth: 640
        minimumHeight: 520
        x: Math.round((Screen.width - width) / 2)
        y: appController.transcriptVisible
           ? Math.round(Math.max(20, transcriptOverlay.y - height - 16))
           : Math.round((Screen.height - height) / 2)
        visible: appController.associationCenterVisible
        title: "数据关联中心"
        color: root.color
        onClosing: function(close) {
            close.accepted = false
            appController.performAssociationAction("center.close", "")
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Label {
                    text: "数据关联中心"
                    color: root.textMain
                    font.pixelSize: 20
                    font.bold: true
                }
                Item { Layout.fillWidth: true }
                Button {
                    visible: appController.associationCenterStage !== "home"
                    Layout.minimumWidth: 104
                    text: "上一步"
                    onClicked: appController.performAssociationAction(
                        "center.back", ""
                    )
                }
            }

            Label {
                Layout.fillWidth: true
                visible: appController.associationCenterStage !== "home"
                text: appController.associationCenterStage === "type"
                      ? "步骤 1/3 · 选择关联类型"
                      : appController.associationCenterStage === "select"
                        ? "步骤 2/3 · 选择一个正例和一个或多个反例"
                        : "步骤 3/3 · 确认并创建关联"
                color: root.textMuted
                font.pixelSize: 12
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: appController.associationCenterStage === "home"
                spacing: 16

                Item { Layout.fillHeight: true }
                Label {
                    Layout.alignment: Qt.AlignHCenter
                    text: appController.associationCenterLastCreatedId === ""
                          ? "每次操作只创建一个独立的 Association"
                          : "已创建关联："
                            + appController.associationCenterLastCreatedId
                    color: appController.associationCenterLastCreatedId === ""
                           ? root.textMuted : "#8BE2C5"
                    font.pixelSize: 14
                }
                Label {
                    Layout.alignment: Qt.AlignHCenter
                    Layout.maximumWidth: 480
                    text: "选择类型、正例和反例后，最后确认才会写入关联。"
                    color: root.textMuted
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
                Button {
                    objectName: "createAssociationButton"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.minimumWidth: 180
                    Layout.preferredHeight: 48
                    text: "创建一次关联"
                    onClicked: appController.performAssociationAction(
                        "center.create", ""
                    )
                }
                Item { Layout.fillHeight: true }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: appController.associationCenterStage === "type"
                spacing: 14
                Button {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 74
                    text: "ASR关联\n听写与编辑指令"
                    onClicked: appController.performAssociationAction(
                        "center.kind", "asr"
                    )
                }
                Button {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 74
                    text: "LLM关联\n正确结果与失败编辑"
                    onClicked: appController.performAssociationAction(
                        "center.kind", "llm"
                    )
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: appController.associationCenterStage === "select"
                         && appController.associationCenterKind === "asr"
                Button {
                    Layout.minimumWidth: 120
                    text: "听写记录"
                    highlighted: appController.associationCenterAsrSubtype
                                 === "dictation_retry"
                    onClicked: appController.performAssociationAction(
                        "center.asrSubtype", "dictation_retry"
                    )
                }
                Button {
                    Layout.minimumWidth: 120
                    text: "编辑指令记录"
                    highlighted: appController.associationCenterAsrSubtype
                                 === "instruction_retry"
                    onClicked: appController.performAssociationAction(
                        "center.asrSubtype", "instruction_retry"
                    )
                }
                Item { Layout.fillWidth: true }
            }

            ListView {
                id: associationCenterList
                objectName: "associationCenterList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: appController.associationCenterStage === "select"
                spacing: 9
                clip: true
                model: appController.associationCenterEntries
                ScrollBar.vertical: ScrollBar { }
                delegate: Rectangle {
                    required property var modelData
                    readonly property string selectedRole: {
                        appController.associationCenterSelectionSummary
                        return appController.associationCenterRole(
                            modelData.interactionId || ""
                        )
                    }
                    width: associationCenterList.width
                    height: centerCardColumn.implicitHeight + 22
                    radius: 11
                    color: selectedRole === "chosen"
                           ? "#253E3540"
                           : selectedRole === "rejected"
                             ? "#3A282D40" : root.panelAlt
                    border.width: selectedRole === "" ? 1 : 2
                    border.color: selectedRole === "chosen"
                                  ? "#4DD4AC"
                                  : selectedRole === "rejected"
                                    ? "#FF646F" : root.border
                    ColumnLayout {
                        id: centerCardColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 11
                        spacing: 5
                        Label {
                            text: (selectedRole === "chosen" ? "✓ 正例 · "
                                  : selectedRole === "rejected" ? "× 反例 · " : "")
                                  + modelData.displayTime
                            color: selectedRole === "chosen"
                                   ? "#8BE2C5"
                                   : selectedRole === "rejected"
                                     ? "#FF9DA5" : root.textMuted
                            font.bold: selectedRole !== ""
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "ASR：" + (modelData.asrText || "（未识别出文本）")
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: Boolean(modelData.resultText)
                            text: "结果：" + modelData.resultText
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                Layout.fillWidth: true
                                text: modelData.statusLabel
                                color: root.textMuted
                                font.pixelSize: 11
                            }
                            Button {
                                Layout.minimumWidth: 74
                                enabled: Boolean(modelData.audioPath)
                                text: "播放"
                                onClicked: appController.playVoiceHistory(
                                    modelData.audioPath
                                )
                            }
                            Button {
                                Layout.minimumWidth: 108
                                text: selectedRole === "chosen"
                                      ? "✓ 已设为正例" : "设为正例"
                                onClicked: appController.performAssociationAction(
                                    "center.chosen", modelData.interactionId || ""
                                )
                            }
                            Button {
                                Layout.minimumWidth: 108
                                text: selectedRole === "rejected"
                                      ? "× 已设为反例" : "设为反例"
                                onClicked: appController.performAssociationAction(
                                    "center.rejected", modelData.interactionId || ""
                                )
                            }
                        }
                    }
                }
            }

            Label {
                Layout.alignment: Qt.AlignHCenter
                visible: appController.associationCenterStage === "select"
                         && associationCenterList.count === 0
                text: "没有尚未关联的可用记录"
                color: root.textMuted
            }

            RowLayout {
                Layout.fillWidth: true
                visible: appController.associationCenterStage === "select"
                Label {
                    Layout.fillWidth: true
                    text: appController.associationCenterSelectionSummary
                    color: appController.associationCenterCanSave
                           ? "#8BE2C5" : root.textMuted
                }
                Button {
                    Layout.minimumWidth: 108
                    text: "加载更早记录"
                    onClicked: appController.performAssociationAction(
                        "center.loadMore", ""
                    )
                }
                Button {
                    Layout.minimumWidth: 96
                    text: "清空选择"
                    onClicked: appController.performAssociationAction(
                        "center.clear", ""
                    )
                }
                Button {
                    Layout.minimumWidth: 128
                    enabled: appController.associationCenterCanSave
                    text: "下一步：确认"
                    onClicked: appController.performAssociationAction(
                        "center.confirm", ""
                    )
                }
            }

            ListView {
                id: associationConfirmationList
                objectName: "associationConfirmationList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: appController.associationCenterStage === "confirm"
                spacing: 10
                clip: true
                model: appController.associationCenterConfirmationEntries
                ScrollBar.vertical: ScrollBar { }
                delegate: Rectangle {
                    required property var modelData
                    width: associationConfirmationList.width
                    height: confirmationColumn.implicitHeight + 22
                    radius: 11
                    color: modelData.role === "chosen" ? "#253E3540" : "#3A282D40"
                    border.width: 2
                    border.color: modelData.role === "chosen" ? "#4DD4AC" : "#FF646F"

                    ColumnLayout {
                        id: confirmationColumn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 11
                        spacing: 6
                        Label {
                            text: modelData.role === "chosen" ? "✓ 正例" : "× 反例"
                            color: modelData.role === "chosen" ? "#8BE2C5" : "#FF9DA5"
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "ASR：" + (modelData.asrText || "（未识别出文本）")
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: Boolean(modelData.resultText)
                            text: "结果：" + modelData.resultText
                            color: root.textMain
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: appController.associationCenterStage === "confirm"
                Label {
                    Layout.fillWidth: true
                    text: (appController.associationCenterKind === "llm"
                           ? "LLM / DPO 关联 · " : "ASR 关联 · ")
                          + appController.associationCenterSelectionSummary
                    color: root.textMuted
                }
                Button {
                    Layout.minimumWidth: 112
                    text: "返回修改"
                    onClicked: appController.performAssociationAction(
                        "center.back", ""
                    )
                }
                Button {
                    objectName: "commitAssociationButton"
                    Layout.minimumWidth: 136
                    text: "确定创建"
                    onClicked: appController.performAssociationAction(
                        "center.commit", ""
                    )
                }
            }
        }
    }
}
