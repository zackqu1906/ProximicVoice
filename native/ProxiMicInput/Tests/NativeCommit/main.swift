import AppKit

var passed = 0
func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}
func test(_ name: String, _ body: () -> Void) {
    body()
    passed += 1
    print("PASS \(name)")
}

// NSTextView is never put in a window, activated, or connected to an external
// application's editor. These tests exercise real AppKit composition semantics.
func editor(_ text: String, selection: NSRange) -> NSTextView {
    let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 100))
    view.string = text
    view.setSelectedRange(selection)
    return view
}
func compose(_ view: NSTextView, _ text: String) {
    view.setMarkedText(text, selectedRange: NSRange(location: (text as NSString).length, length: 0),
                       replacementRange: unspecifiedRange)
}
func commit(_ view: NSTextView, _ text: String, swallowEmptyInsert: Bool = false) -> (marks: Int, inserts: Int) {
    var marks = 0
    var inserts = 0
    CompositionCommitDelivery.send(text, markedRange: { view.markedRange() },
                                   clearMarkedText: { marks += 1; compose(view, "") },
                                   insertText: {
        inserts += 1
        if !($0.isEmpty && swallowEmptyInsert) {
            view.insertText($0, replacementRange: unspecifiedRange)
        }
    })
    return (marks, inserts)
}

test("Empty commit clears only owned preedit and preserves UTF-16 neighbours") {
    let view = editor("前😀尾巴", selection: NSRange(location: 3, length: 0))
    compose(view, "正在听写中")
    check(view.hasMarkedText(), "test did not establish a real AppKit composition")
    let calls = commit(view, "")
    check(view.string == "前😀尾巴", "empty commit removed neighbouring committed text")
    check(!view.hasMarkedText() && view.selectedRange() == NSRange(location: 3, length: 0),
          "empty commit left composition or moved the caret")
    check(calls == (1, 1), "empty commit must clear once and insert once")
}

test("Empty commit still clears preedit when empty insertion is swallowed") {
    let view = editor("前😀尾巴", selection: NSRange(location: 3, length: 0))
    compose(view, "正在听写中")
    _ = commit(view, "", swallowEmptyInsert: true)
    check(view.string == "前😀尾巴" && !view.hasMarkedText(),
          "empty-insert transport left a phantom composition")
    check(view.selectedRange() == NSRange(location: 3, length: 0), "cancel changed caret")
    compose(view, "下一句")
    _ = commit(view, "下一句")
    check(view.string == "前😀下一句尾巴" && !view.hasMarkedText(),
          "cancel prevented the next sentence from being committed")
}

test("Nonempty commit retains ordinary insertion without a preliminary clear") {
    let view = editor("前旧😀后", selection: NSRange(location: 1, length: 3))
    compose(view, "口述")
    let calls = commit(view, "旧😀")
    check(view.string == "前旧😀后" && !view.hasMarkedText(), "original selected text was not restored")
    check(calls == (0, 1), "nonempty restore must be a single insertion")
}

test("Already-cleared composition does not create another marked range") {
    let view = editor("前😀尾巴", selection: NSRange(location: 3, length: 0))
    let calls = commit(view, "")
    check(calls == (0, 1), "adapter created a mark without an existing composition")
    check(view.string == "前😀尾巴" && !view.hasMarkedText() && view.selectedRange().location == 3,
          "no-composition empty commit changed the document")
}

test("Invalid or zero-length marked ranges never trigger a clearing write") {
    let invalid = [unspecifiedRange, NSRange(location: 4, length: 0),
                   NSRange(location: NSNotFound, length: 5), NSRange(location: -1, length: 5),
                   NSRange(location: Int.max - 1, length: 8)]
    for range in invalid {
        var clears = 0
        var inserts = 0
        CompositionCommitDelivery.send("", markedRange: { range },
                                       clearMarkedText: { clears += 1 }, insertText: { _ in inserts += 1 })
        check(clears == 0 && inserts == 1, "invalid marked range caused an extra write: \(range)")
    }
}

final class TextViewClient: CompositionClient {
    let view: NSTextView
    let complete: Bool
    init(_ view: NSTextView, complete: Bool = true) { self.view = view; self.complete = complete }
    func snapshot() throws -> EditorSnapshot {
        let range = view.selectedRange()
        return EditorSnapshot(text: complete ? view.string : nil, selection: range, markedRange: view.markedRange(),
                              selectedText: (view.string as NSString).substring(with: range), documentAccess: true)
    }
    func probe() -> CompositionProbe {
        let range = view.markedRange()
        return CompositionProbe(selection: view.selectedRange(), markedRange: range,
                                markedText: isValidRange(range) ? readText(in: range) : nil)
    }
    func selection() -> NSRange { view.selectedRange() }
    func readText(in range: NSRange) -> String? {
        if range.length == 0 { return "" }
        var actual = unspecifiedRange
        let value = view.attributedSubstring(forProposedRange: range, actualRange: &actual)?.string
        return actual == range ? value : nil
    }
    func mark(_ text: String) { compose(view, text) }
    func commit(_ text: String) {
        CompositionCommitDelivery.send(text, markedRange: { view.markedRange() },
                                       clearMarkedText: { compose(view, "") },
                                       insertText: { view.insertText($0, replacementRange: unspecifiedRange) })
    }
    func replace(_ range: NSRange, with text: String) { view.insertText(text, replacementRange: range) }
}

test("Real NSTextView retains instruction until model result and restores dictation on cancellation") {
    for apply in [false, true] {
        let view = editor("前旧😀尾巴", selection: NSRange(location: 1, length: 3))
        let client = TextViewClient(view)
        let session = try! CompositionSession(client: client, utteranceID: "native-edit", sequence: 1)
        var requests = 0
        session.onEdit = { original, instruction, _ in
            check(original == "前旧😀尾巴" && instruction == "修改指令", "wrong model inputs")
            check(view.string == "前修改指令尾巴" && view.hasMarkedText(), "instruction disappeared while waiting for model")
            requests += 1
        }
        session.update("修改", final: false)
        session.convert()
        session.update("修改指令", final: true)
        check(requests == 1, "real native composition did not reach model")
        let revision = session.revision
        if apply { session.applyEdit(text: "实际结果😀", revision: revision, error: nil) }
        session.cancel()
        check(view.string == "前修改指令尾巴" && session.phase == .dictated, "cancel did not restore dictated sentence")
        session.applyEdit(text: "迟到", revision: revision, error: nil)
        session.cancel()
        check(view.string == "前旧😀尾巴" && session.phase == .undone, "second cancel: body=\(view.string), phase=\(session.phase), error=\(session.error), stage=\(session.readbackStage), selected=\(view.selectedRange()), diagnostics=\(session.readbackDiagnostics)")
    }
}

test("Real NSTextView exact committed-range undo works without whole-document context") {
    let view = editor("前😀后", selection: NSRange(location: 3, length: 0))
    let client = TextViewClient(view, complete: false)
    let session = try! CompositionSession(client: client, utteranceID: "native-local-undo", sequence: 1)
    session.update("听写", final: true)
    check(session.canCancel && session.committedRangeVerified, "native range read did not enable undo")
    session.cancel()
    check(view.string == "前😀后" && session.phase == .undone && !view.hasMarkedText(), "local native undo failed")
}

print("\(passed) native commit tests passed")
