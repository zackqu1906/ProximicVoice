import AppKit

var passed = 0
func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}
func test(_ name: String, _ body: () -> Void) {
    body(); passed += 1; print("PASS \(name)")
}

final class FakeClient: HandoffClient {
    var body = "前旧组合后"
    var selected = NSRange(location: 4, length: 0)
    var marked = NSRange(location: 1, length: 3)
    var markedText: String? = "旧组合"
    var documentAccess = true
    var acknowledges = true
    var calls: [String] = []
    var onProbe: (() -> Void)?
    func snapshot() throws -> EditorSnapshot {
        EditorSnapshot(text: body, selection: selected, markedRange: marked,
                       selectedText: "", documentAccess: documentAccess)
    }
    func probe() -> CompositionProbe {
        onProbe?()
        return CompositionProbe(selection: selected, markedRange: marked, markedText: markedText)
    }
    func selection() -> NSRange { selected }
    func mark(_ text: String) { fatalError("handoff must never overwrite marked text") }
    func replace(_ range: NSRange, with text: String) {
        check(range == marked, "handoff replaced a range other than the exact one read")
        commit(text)
    }
    func commit(_ text: String) {
        check(text == markedText && !text.isEmpty, "handoff guessed or cleared the old text")
        calls.append("commit")
        if acknowledges { marked = unspecifiedRange; markedText = nil }
    }
    func endStaleComposition(at caret: NSRange) {
        check(caret == selected && caret.length == 0 && isValidRange(caret), "reset used a nonzero/foreign range")
        calls.append("zero")
        if acknowledges { marked = unspecifiedRange; markedText = nil }
    }
    static func stale() -> FakeClient {
        let client = FakeClient()
        client.body = "正文😀"
        client.selected = NSRange(location: 0, length: 0)
        client.marked = NSRange(location: 37, length: 5)
        client.markedText = nil
        return client
    }
}
final class TestClock { var value: TimeInterval = 0 }
func tick(_ handoff: CompositionHandoff, _ time: TestClock) {
    time.value += 1.0 / 30.0
    handoff.advance()
}

test("No mark becomes ready without writing") {
    let client = FakeClient(); client.marked = unspecifiedRange
    let handoff = CompositionHandoff(client: client)
    handoff.advance()
    check(handoff.state == .ready && client.calls.isEmpty, "empty editor was modified")
}
test("A readable previous preedit is committed unchanged after two distinct ticks") {
    let client = FakeClient(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    check(client.calls.isEmpty, "initialization wrote to the client")
    handoff.advance(); handoff.advance()
    check(client.calls.isEmpty, "two calls in one tick were treated as stable handoff")
    tick(handoff, time)
    check(client.calls == ["commit"] && handoff.state == .pending, "previous preedit did not commit once")
    tick(handoff, time)
    check(handoff.state == .ready && client.body == "前旧组合后", "commit failed to preserve text")
}
test("A stable detached unreadable cache receives one explicit zero replacement") {
    let client = FakeClient.stale(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time)
    check(client.calls == ["zero"], "stale handoff used the wrong delivery method")
    tick(handoff, time)
    check(handoff.state == .ready && client.body == "正文😀" && client.selected.location == 0,
          "cache reset damaged committed text or caret")
    check(handoff.diagnostics["writes"] as? Int == 1, "write count was not recorded")
    check(handoff.diagnostics["write_action"] as? String == "reset_stale", "ready lost the recorded handoff path")
}
test("Unicode previous composition is checked using UTF-16 and preserved") {
    let client = FakeClient(); client.body = "前旧😀后"; client.markedText = "旧😀"
    client.marked = NSRange(location: 1, length: 3); client.selected = NSRange(location: 4, length: 0)
    let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time); tick(handoff, time)
    check(handoff.state == .ready && client.calls == ["commit"] && client.body == "前旧😀后",
          "UTF-16 composition was corrupted or misclassified")
}
test("Lacking document access never falls back to unspecified empty insertion") {
    let client = FakeClient.stale(); client.documentAccess = false; let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time); time.value = 0.6; handoff.advance()
    check(handoff.state == .failed && client.calls.isEmpty, "unsupported range replacement wrote text")
}
test("Readable preedit without document access waits without a duplicating default insertion") {
    let client = FakeClient(); client.documentAccess = false; let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time); time.value = 0.6; handoff.advance()
    check(handoff.state == .failed && client.calls.isEmpty, "unverified default insertion duplicated old text")
}
test("Unreadable live preedit at the caret remains untouched") {
    let client = FakeClient(); client.markedText = nil; let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time); time.value = 0.6; handoff.advance()
    check(handoff.state == .failed && client.calls.isEmpty, "unknown live preedit was discarded")
}
test("Readable but detached preedit is not committed at another caret") {
    let client = FakeClient(); client.selected = NSRange(location: 0, length: 0); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time); time.value = 0.6; handoff.advance()
    check(handoff.state == .failed && client.calls.isEmpty, "detached readable text was moved or deleted")
}
test("Changed observations restart stabilization") {
    let client = FakeClient(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); client.markedText = "新组合"; tick(handoff, time)
    check(client.calls.isEmpty, "changing preedit was prematurely committed")
    tick(handoff, time)
    check(client.calls == ["commit"], "stable new preedit was not committed")
}
test("System completion during handoff needs no extra commit") {
    let client = FakeClient(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); client.marked = unspecifiedRange; tick(handoff, time)
    check(handoff.state == .ready && client.calls.isEmpty, "system completion was duplicated")
}
test("A delayed acknowledgement never replays the write or changes its strategy") {
    let client = FakeClient.stale(); client.acknowledges = false; let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); tick(handoff, time)
    for _ in 0..<20 { tick(handoff, time) }
    check(handoff.state == .failed && client.calls == ["zero"], "timeout retried a cleanup write")
}
test("Cancellation prevents writes including cancellation inside a client callback") {
    let client = FakeClient.stale(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    handoff.advance(); client.onProbe = { handoff.cancel() }; tick(handoff, time)
    check(handoff.state == .failed && handoff.error.isEmpty && client.calls.isEmpty,
          "reentrant cancellation allowed a handoff write")
    handoff.advance()
    check(client.calls.isEmpty, "cancelled helper revived")
}
test("Synchronous client reentry cannot count as another stable tick or duplicate delivery") {
    let client = FakeClient.stale(); let time = TestClock()
    let handoff = CompositionHandoff(client: client, now: { time.value })
    client.onProbe = { handoff.advance() }
    handoff.advance()
    check(client.calls.isEmpty, "recursive advance was treated as a second observation")
    tick(handoff, time); tick(handoff, time)
    check(handoff.state == .ready && client.calls == ["zero"], "reentry duplicated or suppressed delivery")
}
test("Invalid, noncollapsed, oversized, and mismatched-length observations do not reset") {
    let clients = (0..<4).map { _ in FakeClient.stale() }
    clients[0].selected = unspecifiedRange
    clients[1].selected = NSRange(location: 0, length: 1)
    clients[2].marked = NSRange(location: 37, length: 512 * 1024 + 1)
    clients[3].markedText = "x"
    for client in clients {
        let time = TestClock()
        let handoff = CompositionHandoff(client: client, now: { time.value })
        handoff.advance(); tick(handoff, time); time.value = 0.6; handoff.advance()
        check(handoff.state == .failed && client.calls.isEmpty, "unsafe observation triggered a reset")
    }
}

// Direct, real AppKit tests: these text views never get a window or activation.
func editor(_ selection: NSRange) -> NSTextView {
    let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 100))
    view.string = "前😀尾巴"
    view.setSelectedRange(selection)
    return view
}
test("AppKit empty marking at the current explicit caret preserves text and collapsed selection") {
    for location in [0, 3, 6, 8] {
        let view = editor(NSRange(location: 3, length: 0))
        view.setMarkedText("旧组合", selectedRange: NSRange(location: 3, length: 0), replacementRange: unspecifiedRange)
        // Positions 3 and 6 retain a real mark; moving outside it natively
        // commits the preedit, covering an already-cleared mark as well.
        view.setSelectedRange(NSRange(location: location, length: 0))
        let before = view.string
        let selected = view.selectedRange()
        view.setMarkedText("", selectedRange: NSRange(location: 0, length: 0), replacementRange: selected)
        check(view.string == before && !view.hasMarkedText() && view.selectedRange() == selected,
              "explicit zero replacement deleted or moved AppKit text at \(location)")
    }
}
test("AppKit precise readable-range commit preserves both live and already-committed Unicode text") {
    for live in [true, false] {
        let view = editor(NSRange(location: 3, length: 0))
        view.setMarkedText("旧😀", selectedRange: NSRange(location: 3, length: 0), replacementRange: unspecifiedRange)
        let readRange = view.markedRange()
        let readText = (view.string as NSString).substring(with: readRange)
        if !live { view.unmarkText() }
        let before = view.string
        view.insertText(readText, replacementRange: readRange)
        check(view.string == before && !view.hasMarkedText(), "precise handoff duplicated or dropped Unicode text")
    }
}
test("AppKit empty marking at an explicit zero range preserves Unicode and the middle caret") {
    for selection in [NSRange(location: 0, length: 0), NSRange(location: 1, length: 0),
                      NSRange(location: 3, length: 0), NSRange(location: 5, length: 0)] {
        let view = editor(selection)
        view.setMarkedText("", selectedRange: NSRange(location: 0, length: 0), replacementRange: selection)
        check(view.string == "前😀尾巴" && view.selectedRange() == selection && !view.hasMarkedText(),
              "zero replacement changed ordinary text/selection")
    }
}
test("Negative control proves unspecified replacement can delete the real preedit") {
    let view = editor(NSRange(location: 3, length: 0))
    view.setMarkedText("旧组合", selectedRange: NSRange(location: 3, length: 0), replacementRange: unspecifiedRange)
    view.setMarkedText("", selectedRange: NSRange(location: 0, length: 0), replacementRange: unspecifiedRange)
    check(view.string == "前😀尾巴", "negative control no longer distinguishes explicit replacement")
}
print("\(passed) handoff tests passed")
