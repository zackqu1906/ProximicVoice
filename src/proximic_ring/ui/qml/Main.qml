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

    component OverlayActionButton: Rectangle {
        id: actionButton
        property string title: ""
        property string shortcut: ""
        property color fillColor: "#1A2230"
        property color hoverColor: "#232E40"
        property color pressedColor: "#2B3850"
        property color outlineColor: "#344155"
        property color titleColor: "#F4F7FB"
        property color shortcutColor: "#93A2B8"
        property bool busy: false
        property string busyLabel: "处理中"
        signal triggered()

        implicitHeight: 44
        radius: 10
        color: actionMouse.pressed
               ? pressedColor
               : (actionMouse.containsMouse ? hoverColor : fillColor)
        border.width: 1
        border.color: outlineColor
        Accessible.role: Accessible.Button
        Accessible.name: title + (shortcut.length > 0 ? "，快捷键 " + shortcut : "")

        Behavior on color {
            ColorAnimation { duration: 90 }
        }

        Column {
            anchors.centerIn: parent
            width: parent.width - 14
            spacing: 1

            Text {
                width: parent.width
                height: 17
                text: actionButton.title
                color: actionButton.titleColor
                font.family: root.uiFontFamily
                font.pixelSize: 13
                font.weight: Font.DemiBold
                fontSizeMode: Text.Fit
                minimumPixelSize: 10
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                wrapMode: Text.NoWrap
                clip: true
            }
            Item {
                width: parent.width
                height: 11

                Row {
                    anchors.centerIn: parent
                    spacing: 4

                    Item {
                        id: inlineBusySpinner
                        width: actionButton.busy ? 10 : 0
                        height: 10
                        visible: actionButton.busy

                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            y: 0
                            width: 3
                            height: 3
                            radius: 1.5
                            color: actionButton.titleColor
                        }
                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.bottom: parent.bottom
                            width: 2
                            height: 2
                            radius: 1
                            color: actionButton.shortcutColor
                            opacity: 0.45
                        }
                        RotationAnimator on rotation {
                            from: 0
                            to: 360
                            duration: 720
                            loops: Animation.Infinite
                            running: inlineBusySpinner.visible
                        }
                    }

                    Text {
                        height: 11
                        text: actionButton.busy
                              ? actionButton.busyLabel
                              : actionButton.shortcut
                        color: actionButton.shortcutColor
                        font.family: root.uiFontFamily
                        font.pixelSize: 9
                        font.letterSpacing: 0.4
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.NoWrap
                    }
                }
            }
        }

        MouseArea {
            id: actionMouse
            anchors.fill: parent
            enabled: actionButton.enabled && !actionButton.busy
            hoverEnabled: true
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: actionButton.triggered()
        }
    }

    component SegmentedChoice: Rectangle {
        id: segmentedChoice
        property var options: []
        property int currentIndex: 0
        signal activated(int index)

        implicitHeight: 44
        radius: 12
        color: "#0D121A"
        border.width: 1
        border.color: "#293448"

        Row {
            anchors.fill: parent
            anchors.margins: 4
            spacing: 4

            Repeater {
                model: segmentedChoice.options

                Rectangle {
                    required property int index
                    required property string modelData
                    width: (parent.width - Math.max(0, segmentedChoice.options.length - 1) * parent.spacing)
                           / Math.max(1, segmentedChoice.options.length)
                    height: parent.height
                    radius: 9
                    color: index === segmentedChoice.currentIndex
                           ? "#314472" : "transparent"
                    border.width: index === segmentedChoice.currentIndex ? 1 : 0
                    border.color: "#607DE0"
                    opacity: segmentedChoice.enabled ? 1.0 : 0.45

                    Behavior on color { ColorAnimation { duration: 120 } }

                    Text {
                        anchors.fill: parent
                        anchors.leftMargin: 8
                        anchors.rightMargin: 8
                        text: modelData
                        color: index === segmentedChoice.currentIndex
                               ? "#F4F7FF" : "#8F9CAF"
                        font.family: root.uiFontFamily
                        font.pixelSize: 12
                        font.weight: index === segmentedChoice.currentIndex
                                     ? Font.DemiBold : Font.Normal
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }

                    MouseArea {
                        anchors.fill: parent
                        enabled: segmentedChoice.enabled
                        hoverEnabled: true
                        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: segmentedChoice.activated(index)
                    }
                }
            }
        }
    }

    component SettingsSectionHeader: RowLayout {
        id: sectionHeader
        property string title: ""
        property string badge: ""
        property color accent: root.primary

        spacing: 9

        Rectangle {
            Layout.preferredWidth: 4
            Layout.preferredHeight: 20
            radius: 2
            color: sectionHeader.accent
        }
        Label {
            text: sectionHeader.title
            color: root.textMain
            font.pixelSize: 15
            font.bold: true
        }
        Item { Layout.fillWidth: true }
        Rectangle {
            visible: sectionHeader.badge.length > 0
            Layout.preferredWidth: sectionBadge.implicitWidth + 16
            Layout.preferredHeight: 24
            radius: 12
            color: Qt.rgba(
                sectionHeader.accent.r,
                sectionHeader.accent.g,
                sectionHeader.accent.b,
                0.12
            )
            border.width: 1
            border.color: Qt.rgba(
                sectionHeader.accent.r,
                sectionHeader.accent.g,
                sectionHeader.accent.b,
                0.35
            )
            Label {
                id: sectionBadge
                anchors.centerIn: parent
                text: sectionHeader.badge
                color: sectionHeader.accent
                font.pixelSize: 10
                font.bold: true
            }
        }
    }

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
        title: "运行诊断日志"
        closePolicy: Popup.CloseOnEscape
        onOpened: logArea.refreshLog()

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
                        var viewport = logScroll.contentItem
                        var previousY = viewport ? viewport.contentY : 0
                        var previousCursor = cursorPosition
                        var previousSelectionStart = selectionStart
                        var previousSelectionEnd = selectionEnd
                        var nextText = appController.logText
                        text = nextText.length > 0 ? nextText : "尚未启动"
                        cursorPosition = Math.min(previousCursor, length)
                        if (previousSelectionStart !== previousSelectionEnd) {
                            select(
                                Math.min(previousSelectionStart, length),
                                Math.min(previousSelectionEnd, length)
                            )
                        }
                        Qt.callLater(function() {
                            if (!viewport)
                                return
                            var maximumY = Math.max(0, viewport.contentHeight - viewport.height)
                            viewport.contentY = Math.max(0, Math.min(previousY, maximumY))
                        })
                    }

                    function jumpToLatest() {
                        cursorPosition = length
                        Qt.callLater(function() {
                            var viewport = logScroll.contentItem
                            if (viewport)
                                viewport.contentY = Math.max(
                                    0, viewport.contentHeight - viewport.height
                                )
                        })
                    }

                    Component.onCompleted: refreshLog()
                    Connections {
                        target: appController
                        function onLogChanged() { logArea.refreshLog() }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "窗口保留最近 1000 行；完整诊断日志会持久保存并自动轮转"
                    color: root.textMuted
                    font.pixelSize: 11
                }
                Button {
                    objectName: "openDiagnosticLogDirectoryButton"
                    text: "打开日志目录"
                    onClicked: appController.openDiagnosticLogDirectory()
                }
                Button {
                    objectName: "jumpToLatestLogButton"
                    text: "跳到最新"
                    onClicked: logArea.jumpToLatest()
                }
                Button { text: "清空窗口"; onClicked: appController.clearLog() }
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
                id: runtimeSettingsButton
                objectName: "runtimeSettingsButton"
                text: "设置"
                onClicked: runtimeSettingsDialog.open()
            }

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
                Layout.preferredHeight: 380
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
                            checked: appController.inputMode === "edit"
                            onClicked: appController.inputMode = "edit"
                            ToolTip.visible: hovered
                            ToolTip.text: "读取当前外部文本框，下一段语音作为增删改指令"
                        }
                    }

                    Label {
                        Layout.alignment: Qt.AlignHCenter
                        Layout.maximumWidth: 430
                        text: appController.inputRoutingMode === "auto"
                              ? "自动判断听写或编辑；处理期间可取消，应用后可在文本框旁撤销或改用另一种理解"
                              : (appController.inputMode === "edit"
                              ? "把光标留在目标文本框；修改会直接应用，随后可在文本框旁撤销"
                              : "下一段语音会直接输入到当前文本框，应用后可在文本框旁撤销")
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        horizontalAlignment: Text.AlignHCenter
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
                              + (appController.textProcessing
                                 && appController.interactionState === "processing"
                                 && appController.transcriptText.indexOf("正在处理文本") === 0
                                 ? " · 大模型处理中" : "")
                        color: root.textMuted
                        font.pixelSize: 13
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }

                    Rectangle {
                        id: batteryStatusPill
                        objectName: "batteryStatusPill"
                        Layout.alignment: Qt.AlignHCenter
                        Layout.preferredWidth: Math.min(300, batteryPillContent.implicitWidth + 24)
                        Layout.preferredHeight: 28
                        visible: appController.connected
                        radius: 14
                        color: "#121925"
                        border.width: 1
                        border.color: "#273143"

                        Row {
                            id: batteryPillContent
                            anchors.centerIn: parent
                            spacing: 8

                            Rectangle {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 6
                                height: 6
                                radius: 3
                                color: "#4DD4AC"
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                width: Math.min(132, implicitWidth)
                                text: appController.deviceName
                                color: "#B5BECC"
                                font.family: root.uiFontFamily
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }

                            Rectangle {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 1
                                height: 12
                                color: "#303A4C"
                            }

                            Item {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 22
                                height: 12

                                Rectangle {
                                    id: batteryBody
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 18
                                    height: 10
                                    radius: 2
                                    color: "transparent"
                                    border.width: 1
                                    border.color: appController.batteryAvailable
                                                  ? batteryLevelFill.color
                                                  : "#647085"

                                    Rectangle {
                                        id: batteryLevelFill
                                        objectName: "batteryLevelFill"
                                        x: 2
                                        anchors.verticalCenter: parent.verticalCenter
                                        height: 6
                                        width: appController.batteryAvailable
                                               ? Math.max(2, Math.round(14 * appController.batteryPercentage / 100))
                                               : 2
                                        radius: 1
                                        color: !appController.batteryAvailable
                                               ? "#647085"
                                               : (appController.batteryPercentage <= 15
                                                  ? "#FF7B86"
                                                  : (appController.batteryPercentage <= 35
                                                     ? "#F5B942"
                                                     : "#4DD4AC"))

                                        Behavior on width {
                                            NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
                                        }
                                    }
                                }

                                Rectangle {
                                    anchors.left: batteryBody.right
                                    anchors.leftMargin: 1
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 2
                                    height: 5
                                    radius: 1
                                    color: appController.batteryAvailable
                                           ? batteryLevelFill.color
                                           : "#647085"
                                }
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: appController.batteryAvailable
                                      ? appController.batteryPercentage + "%"
                                      : (appController.batteryQueryComplete
                                         ? "电量不可用"
                                         : "电量读取中")
                                color: appController.batteryAvailable
                                       ? "#D9E0EA"
                                       : root.textMuted
                                font.family: root.uiFontFamily
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                visible: appController.batteryCharging || appController.batteryFull
                                text: appController.batteryCharging ? "充电中" : "已充满"
                                color: "#72D9B8"
                                font.family: root.uiFontFamily
                                font.pixelSize: 10
                            }
                        }
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
                        Button {
                            objectName: "openDataDirectoryButton"
                            Layout.minimumWidth: 128
                            text: "显示数据文件夹"
                            font.pixelSize: 12
                            onClicked: appController.openDataDirectory()
                        }
                        ToolButton { text: "清空"; onClicked: appController.clearVoiceHistory() }
                    }

                    ListView {
                        id: voiceHistoryList
                        objectName: "voiceHistoryList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 8
                        model: appController.voiceHistoryModel
                        ScrollBar.vertical: ScrollBar { }
                        delegate: Rectangle {
                            required property var entry
                            property bool editEntry: entry.mode === "edit"
                            property bool nativeUndoSent: entry.outcome === "native_undo_sent"
                            property bool undoneEntry: entry.outcome === "undone" || nativeUndoSent
                            property bool resultAvailable: Boolean(entry.candidateAvailable)
                                                           || Boolean(entry.candidateText)
                            property bool resultEmpty: Boolean(entry.candidateEmpty)
                            width: voiceHistoryList.width
                            height: Math.max(82, historyContent.implicitHeight + 18)
                            radius: 10
                            color: undoneEntry
                                   ? "#241F1B"
                                   : (editEntry ? "#211E2C" : root.panelAlt)
                            border.color: undoneEntry
                                          ? "#795A35"
                                          : (editEntry ? "#5D507C" : root.border)
                            border.width: editEntry || undoneEntry ? 1.2 : 1

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
                                    spacing: 5
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 8
                                        Label {
                                            objectName: "voiceHistoryMetadata"
                                            Layout.fillWidth: true
                                            text: entry.displayTime + "  ·  "
                                                  + entry.durationLabel
                                                  + (entry.backend ? "  ·  " + entry.backend : "")
                                                  + (entry.application ? "  ·  " + entry.application : "")
                                            color: root.textMuted
                                            font.pixelSize: 10
                                            elide: Text.ElideRight
                                        }
                                        Rectangle {
                                            implicitWidth: historyModeLabel.implicitWidth + 14
                                            implicitHeight: 22
                                            radius: 11
                                            color: undoneEntry
                                                   ? "#4A3521"
                                                   : (editEntry ? "#392F50" : "#203B4D")
                                            border.color: undoneEntry
                                                          ? "#A77943"
                                                          : (editEntry ? "#75639D" : "#37647F")
                                            Label {
                                                id: historyModeLabel
                                                anchors.centerIn: parent
                                                text: nativeUndoSent
                                                      ? "已发送撤销"
                                                      : undoneEntry
                                                      ? (editEntry ? "已撤回编辑" : "已撤回听写")
                                                      : (entry.modeLabel
                                                         || (editEntry ? "编辑指令" : "听写输入"))
                                                color: undoneEntry
                                                       ? "#F1B36A"
                                                       : (editEntry ? "#C9B2F2" : "#85C9ED")
                                                font.pixelSize: 10
                                                font.bold: true
                                            }
                                        }
                                    }
                                    Label {
                                        text: entry.dataSummary
                                        color: entry.hasImu ? "#4DD4AC" : root.textMuted
                                        font.pixelSize: 10
                                    }
                                    Label {
                                        id: historyText
                                        objectName: "voiceHistoryPrimaryText"
                                        Layout.fillWidth: true
                                        text: undoneEntry
                                              ? ((editEntry ? "原编辑指令：" : "原听写内容：")
                                                 + entry.text)
                                              : (editEntry
                                                 ? "编辑指令：" + entry.text
                                                 : "输入内容："
                                                   + (resultAvailable
                                                      ? (resultEmpty
                                                         ? "（空文本）"
                                                         : entry.candidateText)
                                                      : entry.text))
                                        color: entry.recognized ? root.textMain : root.textMuted
                                        font.pixelSize: 13
                                        font.bold: editEntry && !undoneEntry
                                        wrapMode: Text.Wrap
                                    }
                                    Label {
                                        objectName: "voiceHistorySourceText"
                                        Layout.fillWidth: true
                                        visible: !editEntry
                                                 && !undoneEntry
                                                 && resultAvailable
                                                 && !resultEmpty
                                                 && entry.candidateText !== entry.text
                                        text: "识别原文：" + entry.text
                                        color: root.textMuted
                                        font.pixelSize: 11
                                        wrapMode: Text.Wrap
                                    }
                                    Label {
                                        objectName: "voiceHistoryEditSummary"
                                        Layout.fillWidth: true
                                        visible: editEntry
                                                 && (Boolean(entry.editSummary)
                                                     || (!undoneEntry && resultAvailable))
                                        text: (nativeUndoSent ? "原修改摘要："
                                               : undoneEntry ? "已撤回的修改：" : "修改摘要：")
                                              + (entry.editSummary || "修改已应用")
                                        color: undoneEntry ? "#F1B36A" : "#BFA7EA"
                                        font.pixelSize: 12
                                        font.bold: true
                                        wrapMode: Text.Wrap
                                    }
                                    Label {
                                        objectName: "voiceHistoryCandidateText"
                                        Layout.fillWidth: true
                                        visible: editEntry
                                                 && !undoneEntry
                                                 && resultAvailable
                                        text: resultEmpty
                                              ? "修改结果：已清空文本"
                                              : "修改结果：" + entry.candidateText
                                        color: "#C9B2F2"
                                        font.pixelSize: 12
                                        wrapMode: Text.Wrap
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        visible: undoneEntry
                                        spacing: 7
                                        Label {
                                            objectName: "voiceHistoryOutcomeStatus"
                                            text: nativeUndoSent ? "已发送原生撤销" : "已撤回"
                                            color: "#F1B36A"
                                            font.pixelSize: 11
                                            font.bold: true
                                        }
                                        Label {
                                            objectName: "voiceHistoryOutcomeDetail"
                                            Layout.fillWidth: true
                                            text: nativeUndoSent
                                                  ? "结果由目标应用处理，未回读确认"
                                                  : editEntry
                                                  ? "文本已恢复到编辑前状态"
                                                  : "本次听写已从原文本框移除"
                                            color: root.textMuted
                                            font.pixelSize: 11
                                            wrapMode: Text.Wrap
                                        }
                                    }
                                }
                                Button {
                                    objectName: "voiceHistoryOpenLocationButton"
                                    Layout.minimumWidth: 120
                                    Layout.preferredWidth: 120
                                    text: "打开记录文件夹"
                                    font.pixelSize: 12
                                    contentItem: Label {
                                        text: parent.text
                                        color: parent.enabled ? root.textMain : root.textMuted
                                        font: parent.font
                                        horizontalAlignment: Text.AlignHCenter
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideNone
                                    }
                                    onClicked: appController.openVoiceHistoryLocation(entry.recordPath)
                                }
                                Button {
                                    objectName: "voiceHistoryPlayButton"
                                    Layout.minimumWidth: 88
                                    Layout.preferredWidth: 88
                                    text: appController.playingVoicePath === entry.audioPath
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
                                    onClicked: appController.playVoiceHistory(entry.audioPath)
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

        Dialog {
            id: runtimeSettingsDialog
            objectName: "runtimeSettingsDialog"
            parent: Overlay.overlay
            x: Math.round((parent.width - width) / 2)
            y: Math.round((parent.height - height) / 2)
            width: Math.min(720, parent.width - 48)
            height: Math.min(680, parent.height - 48)
            modal: true
            popupType: Popup.Item
            title: "设置"
            closePolicy: Popup.CloseOnEscape
            readonly property bool deviceSettingsLocked:
                appController.connected || appController.busy
            function stage1Sensitivity(threshold) {
                var safe = Math.max(0.001, Math.min(0.05, Number(threshold)))
                return 1 + 9 * Math.log(0.05 / safe) / Math.log(50)
            }
            function thresholdForSensitivity(sensitivity) {
                return 0.05 * Math.pow(0.02, (Number(sensitivity) - 1) / 9)
            }
            background: Rectangle {
                radius: 18
                color: "#0F141D"
                border.width: 1
                border.color: "#2A3548"
            }

            function goBack() {
                runtimeSettingsDialog.close()
            }

            function applyAndClose() {
                // Move focus away from the active editor first so its
                // onEditingFinished/onActiveFocusChanged handler persists the value.
                runtimeSettingsScroll.forceActiveFocus(Qt.OtherFocusReason)
                Qt.callLater(function() { runtimeSettingsDialog.close() })
            }

            contentItem: ScrollView {
                id: runtimeSettingsScroll
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: ScrollBar.AsNeeded
                ColumnLayout {
                    width: runtimeSettingsScroll.availableWidth
                    spacing: 14

                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: runtimeSettingsDialog.deviceSettingsLocked
                              ? "连接期间可调整即时设置；灰色设备和 ASR 设置需断开后修改。"
                              : "设置会自动保存，下次启动继续使用"
                        color: runtimeSettingsDialog.deviceSettingsLocked ? "#AFC0D8" : root.textMuted
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    SettingsSectionHeader {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        title: "语音设备"
                        badge: runtimeSettingsDialog.deviceSettingsLocked ? "断开后修改" : "可修改"
                        accent: "#6F8BFF"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "通过主界面的“选择并连接设备”扫描附近设备，再点击对应设备连接。"
                        color: root.textMain
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    Label { text: "连接质量"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    ComboBox {
                        id: audioEncodingCombo
                        objectName: "audioEncodingCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        Layout.preferredHeight: 44
                        model: ["稳定优先（推荐）", "平衡模式", "原始音质"]
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked
                        currentIndex: Math.max(0, ["opus", "adpcm", "pcm"].indexOf(appController.audioEncoding))
                        onActivated: appController.audioEncoding = ["opus", "adpcm", "pcm"][currentIndex]
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

                    SettingsSectionHeader {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        title: "靠近说话检测"
                        badge: "实时生效"
                        accent: "#55D6AE"
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Label { text: "声音触发灵敏度"; color: root.textMuted; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: Math.round(stage1SensitivitySlider.value) + " / 10"
                            color: root.textMain
                            font.pixelSize: 12
                            font.bold: true
                        }
                    }
                    Slider {
                        id: stage1SensitivitySlider
                        objectName: "stage1SensitivitySlider"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        from: 1
                        to: 10
                        stepSize: 1
                        value: runtimeSettingsDialog.stage1Sensitivity(appController.stage1Threshold)
                        onMoved: appController.stage1Threshold = runtimeSettingsDialog.thresholdForSensitivity(value)
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "数值越高越容易触发；连接期间调整会立即生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    SettingsSectionHeader {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        title: "语音识别"
                        badge: runtimeSettingsDialog.deviceSettingsLocked ? "部分需断开" : "可修改"
                        accent: "#8EA4FF"
                    }
                    ComboBox {
                        id: asrBackendCombo
                        objectName: "asrBackendCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        Layout.preferredHeight: 44
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked
                        model: ["实时识别（推荐）", "本地高精度", "在线识别"]
                        currentIndex: Math.max(0, ["streaming_sensevoice", "funasr_nano", "volcengine"].indexOf(appController.asrBackend))
                        onActivated: appController.asrBackend = ["streaming_sensevoice", "funasr_nano", "volcengine"][currentIndex]
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Label {
                            text: "识别音量增强"
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
                        text: "仅增强送入 ASR 和语音记录的音频，不影响近点模型。默认 0 dB；弱声可先试 +6 dB，过高可能削波。连接期间修改会实时生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; spacing: 10
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked
                        ColumnLayout {
                            Layout.fillWidth: true
                            Label { text: "本地识别性能"; color: root.textMuted; font.pixelSize: 12 }
                            ComboBox {
                                id: asrDeviceCombo
                                objectName: "asrDeviceCombo"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 42
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
                                id: asrLanguageCombo
                                objectName: "asrLanguageCombo"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 42
                                model: ["中文", "自动", "英语", "粤语", "日语", "韩语"]
                                currentIndex: Math.max(0, ["zh", "auto", "en", "yue", "ja", "ko"].indexOf(appController.asrLanguage))
                                onActivated: appController.asrLanguage = ["zh", "auto", "en", "yue", "ja", "ko"][currentIndex]
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
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked

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
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked
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
                        text: "也支持逗号或分号分隔；自动去空和去重，断开并重新连接后生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        visible: appController.asrBackend === "funasr_nano"
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    SettingsSectionHeader {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        title: "使用方式"
                        badge: "即时保存"
                        accent: "#C89BFF"
                    }
                    Label { text: "撤销浮窗"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    SegmentedChoice {
                        id: appliedOverlayStyleCombo
                        objectName: "appliedOverlayStyleCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        options: ["普通浮窗", "极简浮窗"]
                        currentIndex: appController.appliedOverlayStyle === "compact" ? 1 : 0
                        onActivated: function(index) {
                            appController.appliedOverlayStyle = index === 1 ? "compact" : "normal"
                        }
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.appliedOverlayStyle === "compact"
                              ? "仅保留撤销与语音类型转换按钮，占用更少空间。"
                              : "在按钮上方显示本次输入或修改的简短摘要；较长内容会自动省略。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Label { text: "撤销浮窗显示时长"; color: root.textMuted; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: Math.round(appliedOverlayDurationSlider.value) + " 秒"
                            color: root.textMain
                            font.pixelSize: 12
                            font.bold: true
                        }
                    }
                    Slider {
                        id: appliedOverlayDurationSlider
                        objectName: "appliedOverlayDurationSlider"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        from: 1
                        to: 10
                        stepSize: 1
                        value: appController.appliedOverlayDurationSeconds
                        onMoved: appController.appliedOverlayDurationSeconds = Math.round(value)
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "连接设备期间也可即时调整；仅改变浮窗停留时间，不会清除撤销记录。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }

                    Label {
                        text: "类型转换按键"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                    }
                    ComboBox {
                        id: modeCorrectionShortcutCombo
                        objectName: "modeCorrectionShortcutCombo"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        Layout.preferredHeight: 44
                        model: appController.modeCorrectionShortcutOptions
                        currentIndex: Math.max(
                            0,
                            appController.modeCorrectionShortcutOptions.indexOf(
                                appController.modeCorrectionShortcut
                            )
                        )
                        onActivated: appController.modeCorrectionShortcut =
                                     appController.modeCorrectionShortcutOptions[currentIndex]
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "仅在当前结果可以转换时拦截所选功能键；连接期间修改也会立即生效。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Label { text: "听写与指令识别"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    SegmentedChoice {
                        id: inputRoutingModeCombo
                        objectName: "inputRoutingModeCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        options: ["自动判断", "手动选择"]
                        currentIndex: appController.inputRoutingMode === "auto" ? 0 : 1
                        onActivated: function(index) {
                            appController.inputRoutingMode = index === 0 ? "auto" : "manual"
                        }
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.inputRoutingMode === "auto"
                              ? "每段语音结束后先调用所选文本 LLM 判断听写或编辑指令；日志会记录开始时间、结束时间和判断耗时。"
                              : "沿用上方“输入到光标 / 修改当前文本”的固定模式；Alt+1、Alt+2 以及后续手势只负责手动切换。"
                        color: root.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
                    }

                    Switch {
                        id: dictationLlmSwitch
                        objectName: "dictationLlmSwitch"
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "使用大模型整理听写文本"
                        checked: appController.llmEnabled
                        onToggled: appController.llmEnabled = checked
                    }
                    Label {
                        Layout.fillWidth: true
                        Layout.leftMargin: 20
                        Layout.rightMargin: 20
                        text: "关闭后，普通听写会直接使用语音识别结果；编辑指令和自动判断仍会使用文本模型。"
                        color: root.textMuted
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    SettingsSectionHeader {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        title: "文本助手"
                        badge: "下一句话生效"
                        accent: "#F0B85A"
                    }
                    Label { text: "运行方式"; color: root.textMuted; font.pixelSize: 12; Layout.leftMargin: 20 }
                    SegmentedChoice {
                        id: llmProviderCombo
                        objectName: "llmProviderCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        options: ["本地运行", "在线服务"]
                        currentIndex: appController.llmProvider === "local" ? 0 : 1
                        onActivated: function(index) {
                            appController.llmProvider = index === 0 ? "local" : "volcengine"
                        }
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: appController.llmProvider === "local"
                            ? "使用本机文本模型，首次处理时自动启动，内容不会发送到云端。"
                            : "使用在线文本服务处理编辑指令和听写整理。"
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
                        text: "在线模型"
                        color: root.textMuted
                        font.pixelSize: 12
                        Layout.leftMargin: 20
                        visible: appController.llmProvider !== "local"
                    }
                    SegmentedChoice {
                        id: llmModelCombo
                        objectName: "llmModelCombo"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        options: ["豆包 Seed 2.0", "DeepSeek V4"]
                        currentIndex: appController.llmModel === "deepseek-v4-flash-260425" ? 1 : 0
                        onActivated: function(index) {
                            appController.llmModel = index === 0
                                ? "doubao-seed-2-0-lite-260215"
                                : "deepseek-v4-flash-260425"
                        }
                        visible: appController.llmProvider !== "local"
                    }
                    Label {
                        text: "在线服务密钥"
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
                    Rectangle { Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20; height: 1; color: root.border }

                    Switch {
                        id: desktopOutputSwitch
                        objectName: "desktopOutputSwitch"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "识别完成后输入到当前光标"
                        checked: appController.desktopOutputEnabled
                        onToggled: appController.desktopOutputEnabled = checked
                        visible: Qt.platform.os === "windows" || Qt.platform.os === "osx"
                    }
                    Switch {
                        id: pushToTalkSwitch
                        objectName: "pushToTalkSwitch"
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        enabled: !runtimeSettingsDialog.deviceSettingsLocked
                        text: "启用右 Alt 按住说话"
                        checked: appController.pushToTalkEnabled
                        onToggled: appController.pushToTalkEnabled = checked
                        visible: Qt.platform.os === "windows"
                    }
                    Label {
                        Layout.fillWidth: true; Layout.leftMargin: 20; Layout.rightMargin: 20
                        text: "macOS 听写和编辑可作用于当前文本框；首次使用请在系统设置的“隐私与安全性 → 辅助功能”中允许 Proximic Voice。语音处理期间可按 Esc 取消，结果应用后可在文本框旁撤销或切换处理方式；右 Alt 控制仍仅支持 Windows。"
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

            footer: Rectangle {
                implicitHeight: 64
                color: root.panel

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    color: root.border
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 20
                    anchors.rightMargin: 20
                    spacing: 10

                    Label {
                        Layout.fillWidth: true
                        text: appController.connected || appController.busy
                              ? "即时设置会立即或从下一句话开始生效"
                              : "完成设置后点击应用返回"
                        color: root.textMuted
                        font.pixelSize: 11
                    }

                    Button {
                        id: settingsBackButton
                        objectName: "settingsBackButton"
                        Layout.preferredWidth: 88
                        Layout.preferredHeight: 38
                        text: "返回"
                        onClicked: runtimeSettingsDialog.goBack()
                    }

                    Button {
                        id: settingsApplyButton
                        objectName: "settingsApplyButton"
                        Layout.preferredWidth: 88
                        Layout.preferredHeight: 38
                        text: "应用"
                        highlighted: true
                        onClicked: runtimeSettingsDialog.applyAndClose()
                    }
                }
            }
        }
    }

    Window {
        id: transcriptOverlay
        objectName: "transcriptOverlay"
        transientParent: null
        readonly property bool showsRecognizedInstruction:
            appController.transcriptText.indexOf(" · 指令：") >= 0
        readonly property bool showsProcessingModeSwitch:
            showsRecognizedInstruction
            || appController.processingModeCorrectionAvailable
        readonly property string statusText: {
            var marker = appController.transcriptText.indexOf(" · 指令：")
            return marker >= 0
                ? appController.transcriptText.substring(0, marker)
                : appController.transcriptText
        }
        width: showsProcessingModeSwitch
            ? Math.min(620, Screen.width - 32)
            : (appController.interactionCanCancel ? 366 : 300)
        height: 64
        x: Math.round((Screen.width - width) / 2)
        // desktopAvailableHeight excludes the macOS Dock / Windows taskbar.
        // Keep an additional breathing gap so an auto-revealed Dock cannot
        // cover the cancellation button.
        y: Math.round(Math.max(
            12,
            (Screen.desktopAvailableHeight > 0
                ? Screen.desktopAvailableHeight
                : Screen.height) - height - 20
        ))
        visible: appController.transcriptVisible
        color: "transparent"
        flags: (Qt.platform.os === "osx" ? Qt.Window : Qt.Tool)
               | Qt.FramelessWindowHint
               | Qt.WindowStaysOnTopHint
               | Qt.WindowDoesNotAcceptFocus

        Rectangle {
            anchors.fill: parent
            radius: 16
            color: "#E9111620"
            border.color: appController.transcriptFinal ? "#594DD4AC" : "#477892FF"
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 12
                spacing: 10
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 2
                    Label {
                        id: overlayStatusText
                        objectName: "statusOverlayText"
                        Layout.fillWidth: true
                        text: transcriptOverlay.statusText
                        color: "#93A0B4"
                        font.family: root.uiFontFamily
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                    Label {
                        id: overlayText
                        objectName: "asrOverlayText"
                        Layout.fillWidth: true
                        text: appController.transcriptPrimaryText
                        visible: text.length > 0
                        color: appController.transcriptFinal ? "#8BE2C5" : "#F5F7FB"
                        font.family: root.uiFontFamily
                        font.pixelSize: 15
                        elide: Text.ElideRight
                    }
                }
                OverlayActionButton {
                    id: processingSwitchModeButton
                    objectName: "processingSwitchModeButton"
                    // Reserve the slot as soon as the edit instruction is
                    // known. After three seconds only opacity changes, so the
                    // status text and window do not visibly jump or resize.
                    visible: transcriptOverlay.showsProcessingModeSwitch
                    enabled: appController.processingModeCorrectionAvailable
                    opacity: enabled ? 1 : 0
                    Layout.preferredWidth: 132
                    Layout.preferredHeight: 44
                    title: "刚刚是输入内容"
                    shortcut: appController.modeCorrectionShortcut
                    fillColor: "#17302D"
                    hoverColor: "#1D3D38"
                    pressedColor: "#244B44"
                    outlineColor: "#35675F"
                    titleColor: "#A7ECD7"
                    shortcutColor: "#73AD9D"
                    onTriggered: appController.dispatchVoiceAction("switch_mode")
                    Behavior on opacity {
                        NumberAnimation {
                            duration: 180
                            easing.type: Easing.OutCubic
                        }
                    }
                }
                OverlayActionButton {
                    id: cancelUtteranceButton
                    objectName: "cancelUtteranceButton"
                    visible: appController.interactionCanCancel
                    Layout.preferredWidth: 82
                    Layout.preferredHeight: 44
                    title: "取消"
                    shortcut: "Esc"
                    fillColor: "#2A1E24"
                    hoverColor: "#3B252E"
                    pressedColor: "#4B2934"
                    outlineColor: "#75404C"
                    titleColor: "#FFD6DC"
                    shortcutColor: "#D696A0"
                    onTriggered: appController.dispatchVoiceAction("cancel")
                }
            }
        }
    }

    Window {
        id: appliedActionOverlay
        objectName: "appliedActionOverlay"
        property bool userPositioned: false
        property bool systemDragActive: false
        property real userX: 0
        property real userY: 0
        property var rememberedApplicationPositions: ({})
        readonly property string placementKey:
            appController.appliedPopupPlacementKey
        readonly property string applicationKey:
            appController.appliedPopupApplicationKey
        readonly property bool compactStyle:
            appController.appliedOverlayStyle === "compact"
        readonly property bool showsModeCorrection:
            appController.modeCorrectionAvailable
            || appController.modeCorrectionFailed
        transientParent: null
        readonly property bool hasTargetBounds:
            appController.appliedPopupTargetWidth > 0
            && appController.appliedPopupTargetHeight > 0
        readonly property bool hasCaretBounds:
            appController.appliedPopupCaretHeight > 0
        width: Math.min(
            compactStyle
                ? (showsModeCorrection ? 316 : 134)
                : (showsModeCorrection ? 430 : 360),
            Screen.width - 16
        )
        height: compactStyle ? 56 : 120
        function automaticX() {
            if (!hasCaretBounds)
                return Math.round((Screen.width - width) / 2)
            var right = appController.appliedPopupCaretX
                      + appController.appliedPopupCaretWidth + 8
            var left = appController.appliedPopupCaretX - width - 8
            if (right + width <= Screen.width - 8)
                return Math.round(right)
            if (left >= 8)
                return Math.round(left)
            return Math.round(Math.max(8, Math.min(Screen.width - width - 8,
                                                   right)))
        }
        function automaticY() {
            if (!hasCaretBounds)
                return Math.round(Screen.height - height - 96)
            var gap = 16
            var preferred = appController.appliedPopupCaretY - height - gap
            if (hasTargetBounds) {
                var targetLeft = appController.appliedPopupTargetX
                var targetRight = targetLeft + appController.appliedPopupTargetWidth
                var horizontallyOverlaps = x < targetRight
                                           && x + width > targetLeft
                if (horizontallyOverlaps)
                    preferred = Math.min(
                        preferred,
                        appController.appliedPopupTargetY - height - gap
                    )
                if (preferred < 8 && horizontallyOverlaps)
                    preferred = appController.appliedPopupTargetY
                              + appController.appliedPopupTargetHeight + gap
            }
            if (preferred < 8)
                preferred = appController.appliedPopupCaretY
                          + appController.appliedPopupCaretHeight + gap
            return Math.round(Math.max(8, Math.min(Screen.height - height - 8,
                                                   preferred)))
        }
        function finishSystemDrag() {
            userX = appliedActionOverlay.x
            userY = appliedActionOverlay.y
            if (applicationKey !== "") {
                rememberedApplicationPositions[applicationKey] = {
                    "x": userX,
                    "y": userY
                }
            }
            systemDragActive = false
            appliedActionDragSafetyTimer.stop()
            appController.endAppliedOverlayDrag()
        }
        function restoreApplicationPosition() {
            var remembered = applicationKey !== ""
                           ? rememberedApplicationPositions[applicationKey]
                           : undefined
            if (remembered !== undefined) {
                userX = remembered.x
                userY = remembered.y
                userPositioned = true
            } else {
                userPositioned = false
                userX = 0
                userY = 0
            }
        }
        x: userPositioned
            ? Math.round(Math.max(8, Math.min(Screen.width - width - 8, userX)))
            : automaticX()
        y: userPositioned
            ? Math.round(Math.max(8, Math.min(Screen.height - height - 8, userY)))
            : automaticY()
        onXChanged: {
            if (systemDragActive)
                userX = x
        }
        onYChanged: {
            if (systemDragActive)
                userY = y
        }
        onPlacementKeyChanged: {
            if (systemDragActive) {
                systemDragActive = false
                appliedActionDragSafetyTimer.stop()
                appController.endAppliedOverlayDrag()
            }
            // A new utterance in the same application reuses the position the
            // user chose there. A different application keeps an independent
            // position and otherwise starts from its live caret.
            restoreApplicationPosition()
        }
        onApplicationKeyChanged: {
            if (!systemDragActive)
                restoreApplicationPosition()
        }
        visible: appController.appliedActionVisible
        color: "transparent"
        // The controller hides this when the target app leaves the foreground;
        // while visible it must float over that app instead of behind it.
        flags: Qt.Window
               | Qt.FramelessWindowHint
               | Qt.WindowStaysOnTopHint
               | Qt.WindowDoesNotAcceptFocus

        Timer {
            id: appliedActionDragSafetyTimer
            interval: 3000
            repeat: false
            onTriggered: appliedActionOverlay.finishSystemDrag()
        }

        Rectangle {
            anchors.fill: parent
            radius: appliedActionOverlay.compactStyle ? 14 : 16
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "#F2161C27" }
                GradientStop { position: 1.0; color: "#F0111720" }
            }
            border.width: 1
            border.color: appliedActionOverlay.compactStyle ? "#465267" : "#52627A"

            Rectangle {
                visible: !appliedActionOverlay.compactStyle
                width: 3
                height: parent.height - 28
                anchors.left: parent.left
                anchors.leftMargin: 1
                anchors.verticalCenter: parent.verticalCenter
                radius: 1.5
                color: appController.modeCorrectionPending ? "#F0B85A" : "#7892FF"
            }

            Rectangle {
                visible: !appliedActionOverlay.compactStyle
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.leftMargin: 18
                anchors.rightMargin: 18
                height: 1
                color: "#53647D"
                opacity: 0.45
            }

            // Every exposed part of the pill moves the native window.  The
            // button MouseAreas are declared above this background handler and
            // therefore retain their normal click behavior.
            MouseArea {
                id: appliedActionBackgroundDragArea
                objectName: "appliedActionBackgroundDragArea"
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.SizeAllCursor
                onPressed: function(mouse) {
                    appliedActionOverlay.userX = appliedActionOverlay.x
                    appliedActionOverlay.userY = appliedActionOverlay.y
                    appliedActionOverlay.userPositioned = true
                    appliedActionOverlay.systemDragActive = true
                    appController.beginAppliedOverlayDrag()
                    appliedActionOverlay.startSystemMove()
                    appliedActionDragSafetyTimer.restart()
                    mouse.accepted = true
                }
                onReleased: {
                    appliedActionOverlay.finishSystemDrag()
                }
                onCanceled: {
                    appliedActionOverlay.finishSystemDrag()
                }
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: appliedActionOverlay.compactStyle ? 6 : 10
                spacing: appliedActionOverlay.compactStyle ? 0 : 8

                RowLayout {
                    id: appliedActionSummaryRow
                    objectName: "appliedActionSummaryRow"
                    visible: !appliedActionOverlay.compactStyle
                    Layout.fillWidth: true
                    Layout.preferredHeight: visible ? 48 : 0
                    spacing: 10

                    Rectangle {
                        Layout.preferredWidth: 30
                        Layout.preferredHeight: 30
                        radius: 15
                        color: appController.modeCorrectionPending ? "#362D1D" : "#1B2A42"
                        border.width: 1
                        border.color: appController.modeCorrectionPending ? "#7A6231" : "#3D5682"

                        Text {
                            anchors.centerIn: parent
                            text: appController.modeCorrectionPending ? "↻" : "✓"
                            color: appController.modeCorrectionPending ? "#F0C56D" : "#9DB3FF"
                            font.family: root.uiFontFamily
                            font.pixelSize: 16
                            font.weight: Font.DemiBold
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            id: appliedActionTitle
                            objectName: "appliedActionTitle"
                            Layout.fillWidth: true
                            text: appController.appliedActionTitle
                            color: "#F5F7FB"
                            font.family: root.uiFontFamily
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Text {
                            id: appliedActionSummary
                            objectName: "appliedActionSummary"
                            Layout.fillWidth: true
                            text: appController.appliedActionText
                            color: "#AAB6C8"
                            font.family: root.uiFontFamily
                            font.pixelSize: 11
                            elide: Text.ElideRight
                        }
                    }

                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 44
                    spacing: 6

                    Item {
                        id: appliedActionDragSpace
                        objectName: "appliedActionDragSpace"
                        Layout.preferredWidth: appliedActionOverlay.compactStyle ? 26 : 70
                        Layout.fillWidth: !appliedActionOverlay.compactStyle
                        Layout.preferredHeight: 44

                        Grid {
                            visible: appliedActionOverlay.compactStyle
                            anchors.centerIn: parent
                            columns: 2
                            spacing: 3
                            Repeater {
                                model: 6
                                Rectangle {
                                    width: 2
                                    height: 2
                                    radius: 1
                                    color: "#68758A"
                                }
                            }
                        }

                        Row {
                            visible: !appliedActionOverlay.compactStyle
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 6

                            Grid {
                                anchors.verticalCenter: parent.verticalCenter
                                columns: 2
                                spacing: 3
                                Repeater {
                                    model: 6
                                    Rectangle {
                                        width: 2
                                        height: 2
                                        radius: 1
                                        color: "#718097"
                                    }
                                }
                            }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: "拖动"
                                color: "#718097"
                                font.family: root.uiFontFamily
                                font.pixelSize: 10
                            }
                        }
                    }

                    OverlayActionButton {
                        id: undoAppliedButton
                        objectName: "undoAppliedButton"
                        Layout.preferredWidth: appliedActionOverlay.compactStyle ? 90 : 104
                        Layout.preferredHeight: 44
                        title: appController.undoDepth > 1
                               ? "撤销（" + appController.undoDepth + "）"
                               : "撤销"
                        shortcut: "Esc"
                        onTriggered: appController.dispatchVoiceAction("undo")
                    }
                    OverlayActionButton {
                        id: switchModeButton
                        objectName: "switchModeButton"
                        visible: appliedActionOverlay.showsModeCorrection
                        enabled: appController.modeCorrectionAvailable
                                 && !appController.modeCorrectionPending
                        Layout.preferredWidth: 176
                        Layout.preferredHeight: 44
                        title: appController.modeCorrectionFailed
                               ? "指令转换失败"
                               : appController.modeCorrectionLabel
                        shortcut: appController.modeCorrectionShortcut
                        busy: appController.modeCorrectionPending
                        fillColor: appController.modeCorrectionFailed
                                   ? "#382027" : "#17302D"
                        hoverColor: appController.modeCorrectionFailed
                                    ? "#382027" : "#1D3D38"
                        pressedColor: appController.modeCorrectionFailed
                                      ? "#382027" : "#244B44"
                        outlineColor: appController.modeCorrectionFailed
                                      ? "#8D4757" : "#35675F"
                        titleColor: appController.modeCorrectionFailed
                                    ? "#FFB5C1" : "#A7ECD7"
                        shortcutColor: appController.modeCorrectionFailed
                                       ? "#D57C8B" : "#73AD9D"
                        onTriggered: appController.dispatchVoiceAction("switch_mode")
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
                var rightFits = right + width <= Screen.width - 12
                var leftFits = left >= 12
                // The result actions prefer the right side. When both are
                // visible, put this recommendation on the opposite side.
                if (appController.appliedActionVisible && leftFits)
                    preferred = left
                else if (rightFits)
                    preferred = right
                else if (leftFits)
                    preferred = left
                else
                    preferred = appController.associationPopupTargetX
            }
            return Math.round(Math.max(12, Math.min(Screen.width - width - 12,
                                                     preferred)))
        }
        y: {
            var preferred = 72
            if (hasTargetBounds) {
                var right = appController.associationPopupTargetX
                          + appController.associationPopupTargetWidth + 12
                var left = appController.associationPopupTargetX - width - 12
                var sideFits = right + width <= Screen.width - 12 || left >= 12
                if (sideFits) {
                    preferred = appController.associationPopupTargetY
                } else {
                    var above = appController.associationPopupTargetY - height - 12
                    var appliedBelowFits = appController.associationPopupTargetY
                                           + appController.associationPopupTargetHeight
                                           + 12 + appliedActionOverlay.height
                                           <= Screen.height - 20
                    preferred = appliedBelowFits
                                ? above
                                : appController.associationPopupTargetY
                                  + appController.associationPopupTargetHeight + 12
                }
            }
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
