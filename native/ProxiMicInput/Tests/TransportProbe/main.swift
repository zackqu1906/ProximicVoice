import AppKit

// An independent editor for observing the real, out-of-process IMK transport.
// It never inspects or writes another application's editor.
let output = URL(fileURLWithPath: CommandLine.arguments[1])
func record(_ event: [String: Any]) {
    var event = event
    event["time"] = Date().timeIntervalSince1970
    guard let data = try? JSONSerialization.data(withJSONObject: event, options: [.sortedKeys]) else { return }
    if !FileManager.default.fileExists(atPath: output.path) { FileManager.default.createFile(atPath: output.path, contents: nil) }
    guard let handle = try? FileHandle(forWritingTo: output) else { return }
    defer { try? handle.close() }
    _ = try? handle.seekToEnd()
    try? handle.write(contentsOf: data + Data([10]))
}
func numbers(_ range: NSRange) -> [Int] { [range.location == NSNotFound ? -1 : range.location, range.length] }
final class ProbeTextView: NSTextView {
    func trace(_ method: String, _ value: Any, _ replacement: NSRange) {
        let text = (value as? NSAttributedString)?.string ?? (value as? String) ?? ""
        record(["method": method, "replacement": numbers(replacement), "incoming": text,
                "value_class": String(describing: type(of: value)), "before": string,
                "selection": numbers(selectedRange()), "marked": numbers(markedRange())])
    }
    override func insertText(_ value: Any, replacementRange: NSRange) {
        trace("insertText", value, replacementRange)
        super.insertText(value, replacementRange: replacementRange)
        record(["method": "afterInsert", "text": string, "selection": numbers(selectedRange()), "marked": numbers(markedRange())])
    }
    override func setMarkedText(_ value: Any, selectedRange: NSRange, replacementRange: NSRange) {
        trace("setMarkedText", value, replacementRange)
        super.setMarkedText(value, selectedRange: selectedRange, replacementRange: replacementRange)
        record(["method": "afterMark", "text": string, "selection": numbers(self.selectedRange()), "marked": numbers(markedRange())])
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let window = NSWindow(contentRect: NSRect(x: 180, y: 260, width: 760, height: 320), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
window.title = "ProxiMic 输入法通道诊断（独立测试窗口）"
let hint = NSTextField(labelWithString: "请点下面的测试文字，再手动选择 ProxiMic Voice。自动检查只操作这个窗口。")
hint.frame = NSRect(x: 20, y: 270, width: 720, height: 30)
window.contentView!.addSubview(hint)
let view = ProbeTextView(frame: NSRect(x: 20, y: 20, width: 720, height: 240))
view.isRichText = false
view.font = .systemFont(ofSize: 24)
view.string = "今天三点开会"
view.setSelectedRange(NSRange(location: (view.string as NSString).length, length: 0))
window.contentView!.addSubview(view)
window.makeKeyAndOrderFront(nil)
window.makeFirstResponder(view)
app.activate(ignoringOtherApps: true)
record(["method": "launched", "text": view.string])
app.run()
