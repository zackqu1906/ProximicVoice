import Foundation
import Carbon
import AppKit

// Installation never selects a source. Gestures may explicitly select it.
let productID = "com.proximic.inputmethod.ProxiMicVoice"
let installation = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Input Methods/ProxiMicInput.app")

func property(_ source: TISInputSource, _ key: CFString) -> AnyObject? {
    guard let pointer = TISGetInputSourceProperty(source, key) else { return nil }
    return Unmanaged<AnyObject>.fromOpaque(pointer).takeUnretainedValue()
}

func sources() -> [TISInputSource] {
    guard let list = TISCreateInputSourceList(nil, true)?.takeRetainedValue() else { return [] }
    return (0..<CFArrayGetCount(list)).map {
        unsafeBitCast(CFArrayGetValueAtIndex(list, $0), to: TISInputSource.self)
    }.filter { property($0, kTISPropertyBundleID) as? String == productID }
}

func snapshot() -> [String: Any] {
    let items = sources().map { source -> [String: Any] in
        ["id": property(source, kTISPropertyInputSourceID) as? String ?? "",
         "name": property(source, kTISPropertyLocalizedName) as? String ?? "",
         "enabled": property(source, kTISPropertyInputSourceIsEnabled) as? Bool ?? false,
         "selected": property(source, kTISPropertyInputSourceIsSelected) as? Bool ?? false]
    }
    return ["bundle_id": productID, "path": installation.path,
            "installed": Bundle(url: installation)?.bundleIdentifier == productID,
            "sources": items]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

@main
struct InputMethodAdmin {
    static func main() throws {
        let command = CommandLine.arguments.dropFirst().first ?? "status"
        switch command {
        case "status": break
        case "ensure-selected", "refresh-selected":
            guard CommandLine.arguments.count == 4, let pid = Int32(CommandLine.arguments[2]), pid > 0 else {
                fail("ensure-selected requires a foreground PID and bundle ID")
            }
            let modeID = productID + ".dictation"
            selectSourceSession(pid: pid, bundle: CommandLine.arguments[3], modeID: modeID,
                refreshOnly: command == "refresh-selected",
                currentSource: {
                    guard let source = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue() else { return "" }
                    return property(source, kTISPropertyInputSourceID) as? String ?? ""
                }, select: {
                    guard let source = sources().first(where: { property($0, kTISPropertyInputSourceID) as? String == modeID }),
                          property(source, kTISPropertyInputSourceIsEnabled) as? Bool == true,
                          property(source, kTISPropertyInputSourceIsSelectCapable) as? Bool == true else {
                        throw SourceSelectionFailure("语音输入法尚未安装或启用，请先在输入法设置中安装组件")
                    }
                    guard TISSelectInputSource(source) == noErr else { throw SourceSelectionFailure("自动切换语音输入法失败，请重试") }
                })
        case "select":
            // A queued gesture must not switch the input source of a newly focused app.
            if let argument = CommandLine.arguments.dropFirst(2).first {
                guard let pid = Int32(argument),
                      NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
                    fail("前台应用已变化，请重新做切换手势")
                }
            }
            let modeID = productID + ".dictation"
            guard let source = sources().first(where: {
                property($0, kTISPropertyInputSourceID) as? String == modeID
            }) else { fail("未找到 ProxiMic Voice，请先在输入法设置中安装组件") }
            guard property(source, kTISPropertyInputSourceIsEnabled) as? Bool == true,
                  property(source, kTISPropertyInputSourceIsSelectCapable) as? Bool == true else {
                fail("ProxiMic Voice 尚未启用，请在系统键盘设置中添加输入法")
            }
            // Re-selecting an active IME can reset composition in some clients.
            if property(source, kTISPropertyInputSourceIsSelected) as? Bool != true {
                let status = TISSelectInputSource(source)
                guard status == noErr else { fail("切换语音输入法失败：\(status)") }
            }
            guard let current = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(),
                  property(current, kTISPropertyInputSourceID) as? String == modeID else {
                fail("系统未确认输入法切换，请点入文本框后重试")
            }
        case "register":
            guard Bundle(url: installation)?.bundleIdentifier == productID else {
                fail("安装位置不是本产品的输入法组件：\(installation.path)")
            }
            let status = TISRegisterInputSource(installation as CFURL)
            guard status == noErr else { fail("输入法注册失败：\(status)") }
        case "enable":
            let matching = sources().sorted {
                let leftIsMode = (property($0, kTISPropertyInputSourceType) as? String) == (kTISTypeKeyboardInputMode as String)
                let rightIsMode = (property($1, kTISPropertyInputSourceType) as? String) == (kTISTypeKeyboardInputMode as String)
                return !leftIsMode && rightIsMode
            }
            guard !matching.isEmpty else { fail("未找到已注册的 ProxiMic 输入源，请先安装。") }
            for source in matching {
                if property(source, kTISPropertyInputSourceIsEnableCapable) as? Bool == true {
                    let status = TISEnableInputSource(source)
                    guard status == noErr else { fail("输入法启用失败：\(status)") }
                }
            }
        case "disable":
            guard !sources().contains(where: { property($0, kTISPropertyInputSourceIsSelected) as? Bool == true }) else {
                fail("请先手动切回系统拼音或其他输入法，再卸载 ProxiMic。")
            }
            for source in sources().reversed() {
                if property(source, kTISPropertyInputSourceIsEnabled) as? Bool == true {
                    let status = TISDisableInputSource(source)
                    guard status == noErr else { fail("输入法停用失败：\(status)") }
                }
            }
        default:
            fail("用法：InputMethodAdmin [status|register|enable|disable|select [前台进程 ID]]")
        }
        let json = try JSONSerialization.data(withJSONObject: snapshot(), options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(json)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}
