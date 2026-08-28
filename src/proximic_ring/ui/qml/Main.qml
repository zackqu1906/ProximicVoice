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
        modal: true
        popupType: Popup.Item
        title: "安装 NVIDIA GPU 加速"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: appController.installGpuSupport()

        contentItem: Label {
            width: gpuInstallDialog.availableWidth
            text: "安装需要下载数 GB 文件。应用将退出并打开独立安装窗口；安装验证成功后会自动重新启动。是否继续？"
            color: root.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
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
                Layout.preferredHeight: appController.reviewPending ? 420 : 380
                radius: 22
                color: root.panel
                border.color: root.border

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 9

                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "全局语音输入"; color: root.textMain; font.pixelSize: 17; font.bold: true }
                        Item { Layout.fillWidth: true }
                        Label { text: "Alt+1 输入 / Alt+2 修改 / 右 Alt 说话"; color: root.textMuted; font.pixelSize: 12 }
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
                            enabled: !appController.reviewPending
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
                            ToolTip.text: "仅影响“输入到光标”；修改模式始终使用文本 LLM"
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
                        text: appController.inputMode === "edit"
                              ? "把光标留在目标文本框，下一段语音是修改要求；生成预览后再确认"
                              : (appController.llmEnabled
                                 ? "下一段语音经文本 LLM 整理后，输入到当前外部文本框"
                                 : "下一段语音直接采用 ASR 最终结果，不再经过文本 LLM")
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
                            text: "确认应用"
                            visible: appController.reviewCanConfirm
                            onClicked: appController.confirmEdit()
                        }
                        Button {
                            objectName: "cancelEditButton"
                            text: "取消"
                            onClicked: appController.cancelEdit()
                        }
                        Button {
                            objectName: "retryEditButton"
                            text: "重说指令"
                            onClicked: appController.retryEdit()
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
                Layout.fillWidth: true
                Layout.preferredHeight: 250
                radius: 18
                color: root.panel
                border.color: root.border
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 22
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "语音会话记录"; color: root.textMain; font.pixelSize: 15; font.bold: true }
                        Item { Layout.fillWidth: true }
                        ToolButton { text: "清空"; onClicked: appController.clearSessionHistory() }
                    }

                    ScrollView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        TextArea {
                            id: sessionHistoryArea
                            objectName: "sessionHistoryArea"
                            text: appController.sessionHistoryText
                            readOnly: true
                            selectByMouse: true
                            color: root.textMain
                            font.pixelSize: 13
                            wrapMode: TextEdit.Wrap
                            leftPadding: 14
                            rightPadding: 14
                            topPadding: 12
                            bottomPadding: 12
                            placeholderText: "这里仅记录 ASR、LLM 结果以及是否应用；真正的文本始终留在外部应用。"
                            background: Rectangle {
                                color: root.panelAlt
                                radius: 10
                                border.color: root.border
                            }
                            onTextChanged: cursorPosition = length
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
                            background: Rectangle { color: "transparent" }

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
                            onTextChanged: {
                                cursorPosition = length
                            }
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
                              ? "火山引擎是云端识别，不使用本机 CPU 或 GPU。"
                              : appController.gpuStatusText
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
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
                            ? "修改模式始终使用本地 GGUF；输入模式是否二次整理由上方开关决定。首次处理时自动启动，全程离线。"
                            : "修改模式始终使用所选在线模型；输入模式是否二次整理由上方开关决定。Key 只从环境变量读取，不会保存到应用设置。"
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
                        text: "API Key 环境变量名"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    TextField {
                        id: llmApiKeyEnvField
                        objectName: "llmApiKeyEnvField"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmApiKeyEnv
                        placeholderText: "ARK_API_KEY"
                        onEditingFinished: appController.llmApiKeyEnv = text
                        visible: appController.llmProvider !== "local"
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
                        visible: Qt.platform.os === "windows"
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
        objectName: "transcriptOverlay"
        width: Math.min(820, Math.max(360,
            Math.max(overlayText.implicitWidth, editPreviewText.implicitWidth) + 58))
        height: Math.min(420, Math.max(74, overlayContent.implicitHeight + 36))
        x: Math.round((Screen.width - width) / 2)
        y: Screen.height - height - 96
        visible: appController.transcriptVisible
        color: "transparent"
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus

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
                spacing: appController.reviewPending ? 10 : 0

                Label {
                    id: overlayText
                    width: parent.width
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

                Label {
                    width: parent.width
                    visible: appController.reviewCanConfirm
                    text: "修改后："
                    color: "#AEB8C8"
                    font.family: "Microsoft YaHei UI"
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
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 17
                    wrapMode: Text.Wrap
                }
            }
        }
    }

    Window {
        id: feedbackReasonOverlay
        objectName: "feedbackReasonOverlay"
        width: 250
        height: 166
        readonly property real rightSideX: transcriptOverlay.x + transcriptOverlay.width + 12
        x: rightSideX + width <= Screen.width - 16
           ? rightSideX
           : Math.max(16, transcriptOverlay.x + transcriptOverlay.width - width)
        y: rightSideX + width <= Screen.width - 16
           ? Math.max(16, transcriptOverlay.y + transcriptOverlay.height - height)
           : Math.max(16, transcriptOverlay.y - height - 12)
        visible: appController.feedbackReasonVisible
        color: "transparent"
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus

        Rectangle {
            anchors.fill: parent
            radius: 16
            color: "#F2141924"
            border.color: "#637892FF"
            border.width: 1

            Column {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 7

                Label {
                    text: appController.feedbackReasonPrompt
                    color: "#F5F7FB"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 15
                    font.bold: true
                }
                Label {
                    text: "Alt+A   语音识别错误"
                    color: "#DDE5F2"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 13
                }
                Label {
                    text: "Alt+L   大模型理解错误"
                    color: "#DDE5F2"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 13
                }
                Label {
                    text: "Alt+O   其他原因"
                    color: "#DDE5F2"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 13
                }
                Label {
                    text: "10 秒内不选择则不标记"
                    color: "#8F9BAD"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: 11
                }
            }
        }
    }
}
