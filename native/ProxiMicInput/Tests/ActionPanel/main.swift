import AppKit

func check(_ ok: @autoclosure () -> Bool, _ message: String) {
    if !ok() { fputs("FAIL: \(message)\n", stderr); exit(1) }
}

let cases: [(String, String)] = [
    ("辅助功能权限尚未生效。若已授权，请完全退出主程序后重新打开", "辅助功能权限未生效"),
    ("按键控制权限尚未生效。若已授权，请完全退出主程序后重新打开", "按键控制权限未生效"),
    ("输入框未确认本次文字更新，已停止后续写入", "输入框未确认文字更新"),
    ("输入框未确认编辑结果或末尾光标，已停止本次操作", "编辑结果或光标未确认"),
    ("输入框未确认本句选取或撤销结果，已停止本次操作", "选取或撤销结果未确认"),
    ("输入框尚未确认本句撤销", "输入框未确认撤销结果"),
    ("光标或选区已变化，本句已结束", "光标或选区已改变"),
    ("输入法连接已断开，已停止本句", "输入法连接已断开"),
    ("语音识别服务连接超时", "语音识别服务连接超时")
]
for (reason, title) in cases {
    let result = ActionPanelPresentation(phase: .error, error: reason)
    check(result.title == title && result.isError && result.detail == reason, "lost failure cause: \(reason)")
}
print("PASS distinct failures display short causes with complete details")
let unsuccessful = ActionPanelPresentation(phase: .dictated, error: "编辑模型返回结果为空，已保留听写")
check(unsuccessful.isError && unsuccessful.title.contains("结果为空"), "retained dictation hid the edit failure")
print("PASS failed edit after dictation remains an error")
for phase in [CompositionSession.Phase.listening, .editing, .dictated, .edited, .undone] {
    check(!ActionPanelPresentation(phase: phase, error: "").isError, "normal phase is red")
}
let normal = ActionPanelPresentation(phase: .interrupted, error: "鼠标操作已接管本句")
check(!normal.isError && normal.title == "鼠标操作已接管本句", "intentional takeover is misreported")
print("PASS normal completion, cancellation and user takeover are not errors")
let unknown = ActionPanelPresentation(phase: .error, error: String(repeating: "未知服务错误", count: 20))
check(unknown.title.count <= 24 && unknown.detail!.count > 24 && unknown.isError, "unknown error grew the palette or lost details")
check(ActionPanelPresentation(phase: .error, error: "").title.contains("未返回具体原因"), "invented a cause for missing error")
print("PASS unknown long errors stay bounded without inventing a cause")

_ = NSApplication.shared
let palette = ActionPanel()
func find(_ root: NSView, _ id: String) -> NSView? {
    if root.identifier?.rawValue == id { return root }
    return root.subviews.lazy.compactMap { find($0, id) }.first
}
guard let window = NSApp.windows.first(where: { $0.contentView.map { find($0, "status") != nil } ?? false }),
      let content = window.contentView, let tint = find(content, "errorTint"),
      let label = find(content, "status") as? NSTextField else { fatalError("missing palette") }
palette.applyPresentation(ActionPanelPresentation(phase: .error, error: cases[0].0))
check(!tint.isHidden && content.layer?.borderWidth == 1 && label.textColor == .white, "failure palette is not red with readable text")
let red = NSColor(cgColor: tint.layer!.backgroundColor!)!.usingColorSpace(.sRGB)!
check(red.redComponent > red.greenComponent * 3 && red.alphaComponent > 0.9, "error background is not visibly red")
content.layoutSubtreeIfNeeded()
if let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) {
    content.cacheDisplay(in: content.bounds, to: bitmap)
    try bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "/private/tmp/proximic-error-palette.png"))
}
palette.applyPresentation(ActionPanelPresentation(phase: .listening, error: "", empty: true))
check(tint.isHidden && content.layer?.borderWidth == 0 && label.textColor == .labelColor && label.toolTip == nil,
      "next utterance kept stale error styling or tooltip")
palette.hide()
print("PASS native palette becomes red on failure and restores normal appearance")

// Exercise the real panel/session transition: editRequested intentionally stays
// true after success and must not keep the result anchored to the instruction.
final class PanelTextClient: CompositionClient {
    let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 160))
    init() { text.string = "原文"; text.setSelectedRange(NSRange(location: 2, length: 0)) }
    func snapshot() throws -> EditorSnapshot {
        EditorSnapshot(text: text.string, selection: text.selectedRange(), markedRange: text.markedRange(),
            selectedText: (text.string as NSString).substring(with: text.selectedRange()), documentAccess: true)
    }
    func probe() -> CompositionProbe {
        let range = text.markedRange()
        return CompositionProbe(selection: text.selectedRange(), markedRange: range,
            markedText: isValidRange(range, length: (text.string as NSString).length) ? (text.string as NSString).substring(with: range) : nil)
    }
    func selection() -> NSRange { text.selectedRange() }
    func mark(_ value: String) {
        text.setMarkedText(value, selectedRange: NSRange(location: (value as NSString).length, length: 0), replacementRange: unspecifiedRange)
    }
    func commit(_ value: String) { text.insertText(value, replacementRange: unspecifiedRange) }
    func replace(_ range: NSRange, with value: String) { text.insertText(value, replacementRange: range) }
}
let client = PanelTextClient()
let session = try CompositionSession(client: client, utteranceID: "panel-edit", sequence: 1)
let screen = NSScreen.screens[0].visibleFrame
let oldCaret = NSRect(x: screen.midX - 150, y: screen.midY, width: 1, height: 20)
let newCaret = NSRect(x: screen.midX + 40, y: screen.midY - 70, width: 1, height: 20)
session.update("扩写", final: false)
palette.update(session)
palette.position(caret: oldCaret, clientLevel: 0)
let oldOrigin = window.frame.origin
session.convert(); session.update("扩写", final: true)
palette.update(session)
check(session.phase == .editing && !palette.needsPosition, "model waiting did not freeze the panel")
palette.position(caret: newCaret, clientLevel: 0)
check(window.frame.origin == oldOrigin, "pending model adopted a transient selection")
session.applyEdit(text: "扩写后的原文\n第二行", revision: session.revision, error: nil)
check(session.phase == .edited && session.editRequested, "fixture did not retain the edit flag")
palette.update(session)
check(palette.needsPosition, "completed edit flag still blocks position refresh")
palette.position(caret: newCaret, clientLevel: 0)
check(window.frame.origin.x == newCaret.minX && window.frame.origin != oldOrigin,
      "result panel stayed at the old preedit caret")
session.cancel(); palette.update(session)
check(session.phase == .dictated && palette.needsPosition, "undo did not resume positioning")
palette.position(caret: oldCaret, clientLevel: 0)
check(window.frame.origin.x == oldCaret.minX, "undo panel did not follow restored dictation")
palette.hide()
print("PASS native panel freezes during editing, follows the result caret and follows undo")


// Background prewarming must not permanently hide the ready-to-speak palette.
let readySession = try CompositionSession(client: PanelTextClient(), utteranceID: "warm-hidden", sequence: 1)
palette.update(readySession)
NSApp.hide(nil)
RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
check(NSApp.isHidden, "fixture did not hide the accessory process")
let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
palette.position(caret: oldCaret, clientLevel: 0)
RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
check(!NSApp.isHidden && window.isVisible, "background prewarming hid the listening palette")
check(!window.isKeyWindow && !window.isMainWindow, "palette took keyboard focus")
check(NSWorkspace.shared.frontmostApplication?.processIdentifier == front, "unhiding activated the IME")
palette.hide()
print("PASS hidden background process shows listening palette without taking focus")
