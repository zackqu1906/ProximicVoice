import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: settings
    required property var service
    readonly property string menuLanguage: ["auto", "zh-Hans", "zh-Hant", "en"][menuLanguagePicker.currentIndex]
    readonly property string previousMenu: menuLanguage === "auto" ? (service.wechatInfo.previous || "") : service.wechatMenuTitle("previous", menuLanguage)
    readonly property string nextMenu: menuLanguage === "auto" ? (service.wechatInfo.next || "") : service.wechatMenuTitle("next", menuLanguage)
    function wechatShortcut(action) {
        var bindings = service.profiles.wechat || []
        for (var i = 0; i < bindings.length; i++)
            if (bindings[i].action === action) return bindings[i].shortcut
        return ""
    }
    function displayShortcut(shortcut) {
        var names = { "Cmd": "⌘ Command", "Ctrl": "⌃ Control", "Alt": "⌥ Option", "Shift": "⇧ Shift" }
        return shortcut.split("+").map(function(key) { return names[key] || key }).join(" + ")
    }
    spacing: 12
    objectName: "wechatShortcutGuide"
    onVisibleChanged: { if (visible) service.refreshWeChatMenu() }
    ColumnLayout {
        Layout.fillWidth: true
        spacing: 8
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "微信聊天切换：首次使用需添加两项系统快捷键"
            color: "#F5F7FB"; font.bold: true
        }
        Button {
            objectName: "openAppShortcutsButton"
            Layout.fillWidth: true
            text: "打开 App 快捷键设置"
            onClicked: settings.service.openSystemShortcuts()
        }
        Label {
            objectName: "wechatSetupInstructions"
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "1. 打开「App 快捷键」后点击「＋」。\n2. 按下方两项分别填写，每项填完后保存。\n3. 回到微信即可使用手势，无需额外测试或勾选确认。\n\n「菜单标题」决定快捷键执行的操作，必须原样填写，不能自行起名。"
            color: "#8D98AA"; font.pixelSize: 12
        }
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: settings.service.systemSettingsStatus
            visible: text.length > 0; color: "#F0B85A"; font.pixelSize: 12
        }
        Label {
            objectName: "wechatDetectedMenuLabel"
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: (settings.service.wechatInfo.version ? "微信 " + settings.service.wechatInfo.version + " · " : "")
                  + (settings.service.wechatInfo.detected ? settings.service.wechatInfo.languageLabel + "\n" : "")
                  + (settings.service.wechatInfo.message || "")
            color: "#8D98AA"; font.pixelSize: 12
        }
        ComboBox {
            id: menuLanguagePicker
            objectName: "wechatMenuLanguagePicker"
            Layout.fillWidth: true
            model: ["自动识别菜单语言（推荐）", "手动：简体中文", "手动：繁體中文", "手动：English"]
            Accessible.name: "微信菜单语言"
        }
        Button { text: "重新识别微信菜单"; onClicked: settings.service.refreshWeChatMenu() }
        Repeater {
            model: [
                { action: "previous", heading: "第一项 · 上一个聊天", menu: settings.previousMenu, shortcut: settings.wechatShortcut("previous") },
                { action: "next", heading: "第二项 · 下一个聊天", menu: settings.nextMenu, shortcut: settings.wechatShortcut("next") }
            ]
            delegate: Rectangle {
                id: setupRow
                required property var modelData
                Layout.fillWidth: true
                implicitHeight: setupFields.implicitHeight + 24
                radius: 10; color: "#151B27"
                ColumnLayout {
                    id: setupFields
                    anchors.fill: parent; anchors.margins: 12; spacing: 8
                    Label { text: setupRow.modelData.heading; color: "#F5F7FB"; font.bold: true }
                    Label {
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                        text: "应用程序：选择微信（WeChat / Weixin）"
                        color: "#F5F7FB"; font.pixelSize: 12
                    }
                    Label {
                        objectName: "wechatMenuInstruction_" + setupRow.modelData.action
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                        text: "菜单标题：" + (setupRow.modelData.menu || "等待识别，请打开微信后重新识别或选择菜单语言")
                        color: "#F5F7FB"; font.pixelSize: 12
                    }
                    Label {
                        objectName: "wechatShortcutInstruction_" + setupRow.modelData.action
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                        text: "键盘快捷键：点击该输入框，再按下 " + settings.displayShortcut(setupRow.modelData.shortcut)
                        color: "#F5F7FB"; font.pixelSize: 12
                    }
                    Button {
                        text: "复制菜单标题"
                        Accessible.name: "复制" + setupRow.modelData.heading + "菜单标题"
                        enabled: setupRow.modelData.menu.length > 0
                        onClicked: settings.service.copyWeChatMenu(setupRow.modelData.action, settings.menuLanguage)
                    }
                }
            }
        }
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "如果修改上方的按键或微信菜单语言，请同步更新系统里的这两项配置。上滑发送无需此设置。"
            color: "#8D98AA"; font.pixelSize: 12
        }
    }
}
