import AppKit
import InputMethodKit
import Carbon

// A local IMK protocol endpoint backed by an actual, windowless NSTextView.
// It reproduces the two observed bridge behaviours: length()==0 and strict
// range reads, plus dropped empty insertText. No external application is used.
final class StrictIMKClient: NSObject, IMKTextInput {
    let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 100))
    var reads = 0
    var markedReads = 0
    var compositionCoordinateOffset = 0
    var emptyInserts = 0
    var explicitEmptyMarks = 0
    var ignoreEmptyMark = false
    var ignoreReplacementRange = false
    var bundle = "test.strict.imk"
    var staleCompositionRead = false
    var wrongMarkedRange = false
    var relativeCompositionSelection = false
    var cachedSelectionOrigin = 0
    var acceptsEmptyInsert = true
    var preserveCommittedCaret = false
    var ignoreMarkedReplacementRange = false
    var staleDocumentRead = false
    var unavailableDocumentRead = false
    var unselectedCachedSuffix = ""
    var dropNonemptyInsert = false
    var trailingParagraphSentinel = false
    var cachedParagraphSuffix = 0
    var fixedReportedSelection: NSRange?
    var reportedSelections: [NSRange] = []
    var reportedMarkedRanges: [NSRange] = []
    var markCount = 0
    var explicitNonemptyMarks = 0
    var insertCount = 0
    var lastMarkedPayload: NSAttributedString?
    var onInsert: (() -> Void)?
    var candidateRectAtIndex: ((Int) -> NSRect)?
    var candidateRect = NSRect.zero
    var candidateQueries: [Int] = []
    var rectQueries: [NSRange] = []
    var rectangleForRange: ((NSRange) -> (NSRect, NSRange))?
    init(_ text: String, selection: NSRange? = nil) {
        super.init()
        view.string = text
        view.setSelectedRange(selection ?? NSRange(location: (text as NSString).length, length: 0))
    }
    func insertText(_ string: Any!, replacementRange range: NSRange) {
        let text = string as! String
        insertCount += 1
        if dropNonemptyInsert && !text.isEmpty { return }
        if text.isEmpty {
            emptyInserts += 1
            if !acceptsEmptyInsert { return }
        }
        let oldCaret = view.selectedRange()
        let preserve = preserveCommittedCaret && isValidRange(range) && !view.hasMarkedText()
        view.insertText(text, replacementRange: ignoreReplacementRange ? unspecifiedRange : range)
        if preserve { view.setSelectedRange(NSRange(location: min(oldCaret.location, (view.string as NSString).length), length: 0)) }
        onInsert?()
    }
    func setMarkedText(_ string: Any!, selectionRange: NSRange, replacementRange range: NSRange) {
        markCount += 1
        lastMarkedPayload = string as? NSAttributedString
        let text = (string as? NSAttributedString)?.string ?? (string as! String)
        if !text.isEmpty && isValidRange(range) { explicitNonemptyMarks += 1 }
        if text.isEmpty && isValidRange(range) {
            explicitEmptyMarks += 1
            if ignoreEmptyMark { return }
        }
        view.setMarkedText(string!, selectedRange: selectionRange,
                           replacementRange: ignoreReplacementRange || ignoreMarkedReplacementRange ? unspecifiedRange : range)
    }
    func selectedRange() -> NSRange {
        if let selected = fixedReportedSelection { return selected }
        if !reportedSelections.isEmpty { return reportedSelections.removeFirst() }
        if compositionCoordinateOffset != 0 && (view.hasMarkedText() || insertCount == 0) {
            let selected = view.selectedRange()
            return NSRange(location: selected.location + compositionCoordinateOffset, length: selected.length)
        }
        if relativeCompositionSelection && view.hasMarkedText() {
            return NSRange(location: cachedSelectionOrigin, length: view.markedRange().length)
        }
        return staleCompositionRead && view.hasMarkedText() ? view.markedRange() : view.selectedRange()
    }
    func markedRange() -> NSRange {
        markedReads += 1
        if !reportedMarkedRanges.isEmpty { return reportedMarkedRanges.removeFirst() }
        let range = view.markedRange()
        if compositionCoordinateOffset != 0 && view.hasMarkedText() {
            return NSRange(location: range.location + compositionCoordinateOffset, length: range.length)
        }
        return wrongMarkedRange && view.hasMarkedText() ? NSRange(location: range.location + 1, length: range.length) : range
    }
    func length() -> Int { 0 }
    func string(from range: NSRange, actualRange: NSRangePointer!) -> String! {
        reads += 1
        if unavailableDocumentRead { actualRange?.pointee = NSRange(location: 0, length: 0); return nil }
        if !unselectedCachedSuffix.isEmpty && view.selectedRange().length == 0 && !view.hasMarkedText() {
            let cached = (view.string + unselectedCachedSuffix) as NSString
            if range.length > 0 && isValidRange(range, length: cached.length) {
                actualRange?.pointee = range
                return cached.substring(with: range)
            }
        }
        if cachedParagraphSuffix > 0, range.length > 0 {
            let cached = (view.string + String(repeating: "\n", count: cachedParagraphSuffix)) as NSString
            if isValidRange(range, length: cached.length) {
                actualRange?.pointee = range
                return cached.substring(with: range)
            }
        }
        if trailingParagraphSentinel && range.location == (view.string as NSString).length && (range.length == 1 || range.length == 2) {
            actualRange?.pointee = range
            return String(repeating: "\n", count: range.length)
        }
        guard range.length > 0, isValidRange(range, length: (view.string as NSString).length) else {
            actualRange?.pointee = NSRange(location: 0, length: 0)
            return nil
        }
        actualRange?.pointee = range
        if staleDocumentRead || (staleCompositionRead && view.hasMarkedText()) {
            return String(repeating: "旧", count: range.length)
        }
        return (view.string as NSString).substring(with: range)
    }
    func attributedSubstring(from range: NSRange) -> NSAttributedString! {
        var actual = unspecifiedRange
        return string(from: range, actualRange: &actual).map { NSAttributedString(string: $0) }
    }
    func characterIndex(for point: NSPoint, tracking mappingMode: IMKLocationToOffsetMappingMode,
                        inMarkedRange: UnsafeMutablePointer<ObjCBool>!) -> Int { NSNotFound }
    func attributes(forCharacterIndex index: Int, lineHeightRectangle: UnsafeMutablePointer<NSRect>!) -> [AnyHashable: Any]! {
        candidateQueries.append(index)
        lineHeightRectangle?.pointee = candidateRectAtIndex?(index) ?? candidateRect
        return [:]
    }
    func validAttributesForMarkedText() -> [Any]! { [] }
    func overrideKeyboard(withKeyboardNamed name: String!) { fatalError("must not select a keyboard") }
    func selectMode(_ identifier: String!) { fatalError("must not select an input source") }
    func supportsUnicode() -> Bool { true }
    func bundleIdentifier() -> String! { bundle }
    func windowLevel() -> Int32 { 0 }
    func supportsProperty(_ property: TSMDocumentPropertyTag) -> Bool { true }
    func uniqueClientIdentifierString() -> String! { "test-local" }
    func firstRect(forCharacterRange range: NSRange, actualRange: NSRangePointer!) -> NSRect {
        rectQueries.append(range)
        let result = rectangleForRange?(range) ?? (.zero, unspecifiedRange)
        actualRange?.pointee = result.1
        return result.0
    }
}

var passed = 0
func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}
func test(_ name: String, _ work: () throws -> Void) {
    do { try work(); passed += 1; print("PASS \(name)") }
    catch { fatalError("\(name): \(error)") }
}

// IMK's candidate rectangle can remain valid at the composition start while
// document-range geometry follows the actual insertion point on later lines.
let geometryScreen = NSScreen.screens[0].visibleFrame
let candidatePoint = NSPoint(x: geometryScreen.midX - 200, y: geometryScreen.midY)
let candidateRect = NSRect(origin: candidatePoint, size: NSSize(width: 1, height: 20))
test("Every preedit revision uses the system raw-text marking for Chinese and digits") {
    let proxy = StrictIMKClient("原文")
    let markingController = IMKInputController()
    var requestedRanges: [NSRange] = []
    var generatedAttributes: [NSAttributedString.Key: Any] = [:]
    let adapter = NativeInputClient(proxy, preeditAttributes: { range in
        requestedRanges.append(range)
        generatedAttributes = markingController.mark(forStyle: kTSMHiliteSelectedRawText, at: range)! as! [NSAttributedString.Key: Any]
        return generatedAttributes
    })
    for text in ["中文", "中文123", "修改后的中文123\n第二行😀", "中"] {
        adapter.mark(text)
        guard let payload = proxy.lastMarkedPayload else { fatalError("preedit styling was delegated to the client's inconsistent defaults") }
        check(payload.string == text && payload.length == (text as NSString).length, "styled preedit changed content")
        let expectedRange = NSRange(location: 0, length: payload.length)
        check(requestedRanges.last == expectedRange, "system marking did not receive this full revision")
        // IMK allocates a new clause segment for each call. Compare with the
        // dictionary produced for this exact revision, not a second request.
        let expected = generatedAttributes
        for i in 0..<payload.length {
            let actual = payload.attributes(at: i, effectiveRange: nil)
            check(NSDictionary(dictionary: actual).isEqual(to: expected), "preedit changed the system marking dictionary")
            check(((payload.attribute(.underlineStyle, at: i, effectiveRange: nil) as? NSNumber)?.intValue ?? 0) != 0,
                  "a Chinese/Latin/UTF-16 character lost its preedit underline")
        }
        check(payload.attribute(.font, at: 0, effectiveRange: nil) == nil && payload.attribute(.foregroundColor, at: 0, effectiveRange: nil) == nil,
              "preedit overrode client text appearance")
        check(proxy.view.hasMarkedText(), "styling committed the composition")
    }
    adapter.commit("中")
    check(proxy.view.string == "原文中" && !proxy.view.hasMarkedText(), "styled preedit did not commit normally")
    let style = proxy.view.textStorage?.attribute(.underlineStyle, at: 2, effectiveRange: nil) as? NSNumber
    check(style == nil || style?.intValue == 0, "preedit underline leaked into committed text")
    adapter.mark("再次听写")
    adapter.commit("")
    check(proxy.view.string == "原文中" && !proxy.view.hasMarkedText(), "styled cancellation left text or a composition")
}

test("Real bridge trace: one-past-end is empty, last character follows growth and wrap") {
    let proxy = StrictIMKClient("")
    let adapter = NativeInputClient(proxy)
    // Capture reproduced in WeChat: document geometry is malformed across
    // IMK, and attributes(length) is zero while attributes(length-1) moves.
    proxy.rectangleForRange = { _ in (NSRect(x: 1.6e-314, y: 6.2e10, width: 1.6e-314, height: 1), NSRange(location: 0, length: 0)) }
    var previous: NSRect?
    for (length, dx, dy) in [(5, 41, 0), (10, 137, 0), (18, 207, 0), (28, 310, 0), (36, 407, 0), (42, 484, 0), (49, 55, -17)] {
        adapter.mark(String(repeating: "字", count: length))
        let live = candidateRect.offsetBy(dx: Double(dx), dy: Double(dy))
        proxy.candidateRectAtIndex = { $0 == length - 1 ? live : .zero }
        let result = adapter.caret(compositionLength: length)
        check(result == live, "queried one-past-end and retained the old palette position")
        if let previous { check(result != previous, "bridge fixture stopped following") }
        previous = result
    }
    check(proxy.candidateQueries == [4, 9, 17, 27, 35, 41, 48], "inline character index is not zero-based")
    check(proxy.reads == 0, "positioning unexpectedly read document text")
}
test("Late layout does not stop later caret updates") {
    let proxy = StrictIMKClient("")
    let adapter = NativeInputClient(proxy)
    adapter.mark("正在换行")
    proxy.candidateRectAtIndex = { _ in .zero }
    check(adapter.caret(compositionLength: 4) == nil, "missing layout fabricated a caret")
    let next = candidateRect.offsetBy(dx: -20, dy: -17)
    proxy.candidateRectAtIndex = { $0 == 3 ? next : .zero }
    check(adapter.caret(compositionLength: 4) == next, "later layout never refreshed caret")
}

test("Caret follows preedit tail instead of stationary candidate anchor") {
    let proxy = StrictIMKClient("已有原文")
    proxy.candidateRect = candidateRect
    let adapter = NativeInputClient(proxy)
    var lastRect: NSRect?
    for (step, partial) in ["今天", "今天下午", "今天下午开会换行继续说", "今天下午开会换行继续说😀"].enumerated() {
        adapter.mark(partial)
        let expectedRange = NSRange(location: NSMaxRange(proxy.view.markedRange()), length: 0)
        let expectedRect = NSRect(x: candidatePoint.x + Double(step % 2) * 90,
                                  y: candidatePoint.y - Double(step / 2) * 24,
                                  width: 0, height: 20)
        proxy.rectangleForRange = { requested in
            check(requested == expectedRange, "caret query used a relative or cached selection offset")
            return (expectedRect, requested)
        }
        let result = adapter.caret(compositionLength: (partial as NSString).length)
        check(result == expectedRect, "valid but stationary candidate rectangle hid the live caret")
        if let lastRect { check(result != lastRect, "caret stopped on growth or wrap") }
        lastRect = result
    }
    check(proxy.candidateQueries.isEmpty && proxy.reads == 0, "caret tracking read text or queried unnecessary fallback")
}
test("Composition geometry uses live marked range with cached relative selection") {
    let proxy = StrictIMKClient("已有原文")
    proxy.relativeCompositionSelection = true
    let adapter = NativeInputClient(proxy)
    adapter.mark("正在听写")
    let expected = NSRange(location: NSMaxRange(proxy.view.markedRange()), length: 0)
    proxy.rectangleForRange = { requested in
        check(requested == expected, "cached full-composition selection displaced caret")
        return (candidateRect, requested)
    }
    check(adapter.caret(compositionLength: 4) == candidateRect, "relative-selection client lost caret")
}
test("No composition queries current insertion point") {
    let proxy = StrictIMKClient("已有原文")
    proxy.candidateRect = candidateRect.offsetBy(dx: -80, dy: 0)
    proxy.rectangleForRange = { (candidateRect, $0) }
    check(NativeInputClient(proxy).caret(compositionLength: 0) == candidateRect, "idle caret used candidate start")
    check(proxy.rectQueries == [NSRange(location: 4, length: 0)], "idle caret queried wrong range")
}
test("Unavailable or mismatched caret geometry uses native candidate fallback") {
    for wrongRange in [false, true] {
        let proxy = StrictIMKClient("")
        proxy.candidateRect = candidateRect
        let adapter = NativeInputClient(proxy)
        adapter.mark("正在听写")
        proxy.rectangleForRange = { requested in
            wrongRange ? (candidateRect.offsetBy(dx: 99, dy: -40), NSRange(location: 0, length: requested.location)) : (.zero, unspecifiedRange)
        }
        check(adapter.caret(compositionLength: 4) == candidateRect, "invalid range geometry was treated as current caret")
        check(proxy.candidateQueries == [3], "native inline fallback used wrong index")
    }
}
test("Caret geometry rejects off-screen and nonfinite rectangles") {
    for rectangle in [NSRect(x: Double.nan, y: 0, width: 0, height: 20), NSRect(x: 1e9, y: 1e9, width: 0, height: 20)] {
        let proxy = StrictIMKClient("")
        proxy.rectangleForRange = { (rectangle, $0) }
        check(NativeInputClient(proxy).caret(compositionLength: 0) == nil, "invalid rectangle leaked to palette")
    }
}

test("Real NSTextView follows growing marked text across a wrapped line") {
    let proxy = StrictIMKClient("已有原文：")
    let window = NSWindow(contentRect: NSRect(x: candidatePoint.x, y: candidatePoint.y - 150, width: 180, height: 150),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = proxy.view
    proxy.view.font = .systemFont(ofSize: 16)
    proxy.view.isHorizontallyResizable = false
    proxy.view.textContainer?.widthTracksTextView = true
    proxy.view.textContainer?.containerSize = NSSize(width: 180, height: 10000)
    proxy.candidateRect = candidateRect
    proxy.rectangleForRange = { requested in
        proxy.view.layoutManager?.ensureLayout(for: proxy.view.textContainer!)
        var actual = unspecifiedRange
        let rectangle = proxy.view.firstRect(forCharacterRange: requested, actualRange: &actual)
        return (rectangle, actual)
    }
    let adapter = NativeInputClient(proxy)
    var rectangles: [NSRect] = []
    for text in ["今天", "今天下午", "今天下午我们继续听写这一段内容，直到它超过一行并且自动换行。"] {
        adapter.mark(text)
        guard let rectangle = adapter.caret(compositionLength: (text as NSString).length) else {
            fatalError("native text layout supplied no caret geometry")
        }
        rectangles.append(rectangle)
    }
    check(rectangles[0].minX != rectangles[1].minX, "native caret did not move horizontally")
    check(rectangles[2].minY < rectangles[1].minY, "native caret did not follow line wrap")
    check(proxy.candidateQueries.isEmpty, "native text layout fell back to stationary anchor")
    window.close()
}

test("Negative controls reproduce logged nil context and swallowed committed undo") {
    let proxy = StrictIMKClient("原文听写")
    proxy.acceptsEmptyInsert = false
    var actual = unspecifiedRange
    check(proxy.string(from: NSRange(location: 0, length: 32768), actualRange: &actual) == nil,
          "oversized read did not reproduce bridge failure")
    proxy.insertText("", replacementRange: NSRange(location: 2, length: 2))
    check(proxy.view.string == "原文听写", "empty insertion did not reproduce bridge failure")
}

test("Production adapter reads original when oversized query is rejected") {
    for caret in [0, 3, 7] {
        let proxy = StrictIMKClient("前😀中间尾巴", selection: NSRange(location: caret, length: 0))
        let adapter = NativeInputClient(proxy)
        let snapshot = try adapter.snapshot()
        check(!snapshot.complete && snapshot.readableContext?.text == proxy.view.string,
              "readable original was lost or labeled complete")
        check(snapshot.readableContext?.range == NSRange(location: 0, length: 7), "wrong UTF16 context range")
        check(proxy.reads <= 12, "short input performed excessive queries")
    }
}

test("Codex stale substring cache does not block partials, final or live cancellation") {
    for cancel in [false, true] {
        let proxy = StrictIMKClient("原文")
        proxy.bundle = "com.openai.codex"
        proxy.staleCompositionRead = true
        let adapter = NativeInputClient(proxy)
        let session = try CompositionSession(client: adapter, utteranceID: "codex", sequence: 1)
        let reads = proxy.reads
        for text in ["今", "今天", "今天下午😀\n开会"] {
            // Real Codex switched from document [141,1] to relative [0,4].
            proxy.relativeCompositionSelection = text != "今"
            proxy.cachedSelectionOrigin = text == "今天" ? 0 : 6
            session.update(text, final: false)
            session.checkSelection()
            check(session.phase == .listening && !session.awaitingReadback && proxy.view.string == "原文" + text,
                  "Codex partial blocked on stale text cache")
        }
        check(proxy.reads == reads, "streaming still queries cached document text")
        if cancel { session.cancel() }
        else { session.update("今天下午😀\n开会。", final: true) }
        check(!session.awaitingReadback && !proxy.view.hasMarkedText(), "Codex failed to end marked text")
        check(cancel ? session.phase == .undone && proxy.view.string == "原文" : session.phase == .dictated && proxy.view.string == "原文今天下午😀\n开会。", "Codex result wrong")
    }
}

test("Codex only normalizes the full relative composition selection") {
    let proxy = StrictIMKClient(String(repeating: "原", count: 141))
    proxy.bundle = "com.openai.codex"
    let adapter = NativeInputClient(proxy)
    proxy.view.setMarkedText("今天下午", selectedRange: NSRange(location: 4, length: 0), replacementRange: unspecifiedRange)
    proxy.relativeCompositionSelection = true
    check(adapter.probe().selection == NSRange(location: 141, length: 4), "relative range not normalized")
    check(adapter.selection() == NSRange(location: 141, length: 4), "poll used unnormalized range")
    proxy.cachedSelectionOrigin = 6
    check(adapter.probe().selection == NSRange(location: 141, length: 4), "cached selection origin was not normalized")
    proxy.relativeCompositionSelection = false
    proxy.view.setSelectedRange(NSRange(location: 1, length: 0))
    check(adapter.selection() == NSRange(location: 1, length: 0), "arbitrary caret was reinterpreted")
}

test("Codex committed undo uses empty insertText, then the next sentence streams normally") {
    let proxy = StrictIMKClient("原文")
    proxy.bundle = "com.openai.codex"
    proxy.acceptsEmptyInsert = true
    proxy.ignoreEmptyMark = true
    proxy.staleCompositionRead = true
    let adapter = NativeInputClient(proxy)
    let s = try CompositionSession(client: adapter, utteranceID: "codex-undo", sequence: 1)
    s.update("听写", final: true); s.cancel()
    check(s.phase == .undone && proxy.view.string == "原文" && proxy.explicitEmptyMarks == 0,
          "Codex committed deletion used composition cancellation")
    let next = try CompositionSession(client: adapter, utteranceID: "codex-next", sequence: 1)
    next.update("继续", final: false); next.update("继续听写", final: true)
    check(next.phase == .dictated && proxy.view.string == "原文继续听写", "next sentence blocked after undo")
}

test("Negative control reproduces VS Code cancelling composition without deleting committed text") {
    let proxy = StrictIMKClient("原文" + String(repeating: "字", count: 8))
    proxy.acceptsEmptyInsert = true
    proxy.ignoreMarkedReplacementRange = true
    proxy.setMarkedText("", selectionRange: NSRange(location: 0, length: 0),
                        replacementRange: NSRange(location: 2, length: 8))
    check((proxy.view.string as NSString).length == 10 && proxy.view.selectedRange() == NSRange(location: 10, length: 0),
          "negative control did not retain the logged pre-undo caret and text")
}

test("VS Code committed undo deletes only its sentence and permits the next dictation") {
    for bundle in ["com.microsoft.VSCode", "com.microsoft.VSCodeInsiders"] {
        for prefix in ["", "已有文字", "前😀\n"] {
            let suffix = "后面的原文"
            let proxy = StrictIMKClient(prefix + suffix, selection: NSRange(location: (prefix as NSString).length, length: 0))
            proxy.bundle = bundle
            proxy.acceptsEmptyInsert = true
            proxy.ignoreMarkedReplacementRange = true
            let adapter = NativeInputClient(proxy)
            let s = try CompositionSession(client: adapter, utteranceID: "vscode-undo", sequence: 1)
            s.update("听写😀", final: false); s.update("听写😀。", final: true)
            s.cancel()
            check(s.phase == .undone && !s.awaitingReadback && proxy.view.string == prefix + suffix,
                  "VS Code committed undo still used composition cancellation")
            check(proxy.explicitEmptyMarks == 0 && proxy.emptyInserts == 1 && !proxy.view.hasMarkedText(),
                  "VS Code undo left a mark or issued a second deletion")
            let next = try CompositionSession(client: adapter, utteranceID: "vscode-next", sequence: 2)
            next.update("下一句", final: true)
            check(next.phase == .dictated && proxy.view.string == prefix + "下一句" + suffix,
                  "VS Code undo moved the next dictation out of its original range")
        }
    }
}

test("VS Code can undo past an edited history entry without abandoning earlier dictation") {
    let proxy = StrictIMKClient("原文")
    proxy.bundle = "com.microsoft.VSCode"
    proxy.acceptsEmptyInsert = true
    proxy.ignoreMarkedReplacementRange = true
    let adapter = NativeInputClient(proxy)
    let history = CompositionUndoHistory()
    history.configure(enabled: true)
    let first = try CompositionSession(client: adapter, utteranceID: "first", sequence: 1)
    first.update("甲😀", final: true); history.settled(first)
    let edit = try CompositionSession(client: adapter, utteranceID: "edit", sequence: 2)
    history.begin(edit.original)
    edit.update("扩写", final: false); edit.convert(); edit.update("扩写", final: true)
    edit.applyEdit(text: "扩写后的完整内容。", revision: edit.revision, error: nil)
    check(edit.phase == .edited, "historical edit precondition failed")
    history.settled(edit)
    let last = try CompositionSession(client: adapter, utteranceID: "last", sequence: 3)
    history.begin(last.original)
    last.update("下一句", final: true); last.cancel()
    check(last.phase == .undone && proxy.view.string == "扩写后的完整内容。", "last dictation undo failed")
    guard let editRecord = history.pop() else { fatalError("edited record was lost") }
    let restoredEdit = CompositionSession(client: adapter, undo: editRecord, utteranceID: "undo-edit", sequence: 4, revision: 10)
    restoredEdit.cancel()
    check(restoredEdit.phase == .dictated && proxy.view.string == "原文甲😀扩写", "edit did not restore its dictated instruction")
    restoredEdit.cancel()
    check(restoredEdit.phase == .undone && proxy.view.string == "原文甲😀", "restored instruction could not be undone")
    guard let firstRecord = history.pop() else { fatalError("earlier dictation was lost") }
    let restoredFirst = CompositionSession(client: adapter, undo: firstRecord, utteranceID: "undo-first", sequence: 5, revision: 20)
    restoredFirst.cancel()
    check(restoredFirst.phase == .undone && proxy.view.string == "原文" && proxy.explicitEmptyMarks == 0,
          "undo stalled when crossing the edit record")
}

test("VS Code ignores a failed committed deletion only once and never marks undo successful") {
    let proxy = StrictIMKClient("原文")
    proxy.bundle = "com.microsoft.VSCode"
    proxy.ignoreMarkedReplacementRange = true
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    proxy.acceptsEmptyInsert = false
    let s = try CompositionSession(client: adapter, utteranceID: "vscode-dropped-undo", sequence: 1, clock: { now })
    s.update("听写", final: true); s.cancel()
    check(s.awaitingReadback, "dropped deletion was falsely acknowledged")
    now = 0.6; s.advanceReadback(); s.cancel(); s.advanceReadback()
    check(s.phase == .error && proxy.view.string == "原文听写" && proxy.emptyInserts == 1 && proxy.explicitEmptyMarks == 0,
          "failed undo was retried, relabeled as success, or used a marked-text fallback")
}

test("Codex editing starts despite stale composing context, then verifies original after model response") {
    for cancel in [true, false] {
        let proxy = StrictIMKClient("明天三点开会")
        proxy.bundle = "com.openai.codex"
        proxy.staleCompositionRead = true
        proxy.relativeCompositionSelection = true
        let adapter = NativeInputClient(proxy)
        let s = try CompositionSession(client: adapter, utteranceID: "codex-edit", sequence: 1)
        var calls = 0
        s.onEdit = { original, instruction, _ in
            check(original == "明天三点开会" && instruction == "改成四点", "model input included stale composition cache")
            check(proxy.view.string == "明天三点开会改成四点" && proxy.view.hasMarkedText(), "instruction removed before model")
            calls += 1
        }
        s.update("改成", final: false); s.convert(); s.update("改成四点", final: true)
        check(calls == 1 && s.phase == .editing, "Codex model call was blocked by stale live readback")
        if cancel {
            s.cancel()
            check(s.phase == .dictated && proxy.view.string == "明天三点开会改成四点", "waiting cancel lost dictation")
        } else {
            s.applyEdit(text: "明天四点开会", revision: s.revision, error: nil)
            check(s.phase == .edited && proxy.view.string == "明天四点开会", "Codex edit not applied")
        }
    }
}

test("Negative control reproduces an expanded committed replacement retaining the old caret") {
    let original = String(repeating: "原", count: 14)
    let result = String(repeating: "新", count: 32)
    let proxy = StrictIMKClient(original)
    proxy.preserveCommittedCaret = true
    proxy.insertText(result, replacementRange: NSRange(location: 0, length: 14))
    check(proxy.view.string == result && proxy.view.selectedRange() == NSRange(location: 14, length: 0),
          "logged text-success/caret-failure was not reproduced")
}

test("Geometry-only composition still rejects wrong ranges and manual caret takeover") {
    for wrongRange in [true, false] {
        let proxy = StrictIMKClient("原文")
        proxy.bundle = "com.openai.codex"
        let adapter = NativeInputClient(proxy)
        var now: TimeInterval = 0
        let session = try CompositionSession(client: adapter, utteranceID: "codex-guard", sequence: 1, clock: { now })
        proxy.wrongMarkedRange = wrongRange
        session.update("今天", final: false)
        if wrongRange {
            now = 1.1; session.advanceReadback()
            check(session.phase == .error, "wrong composition range granted ownership")
        } else {
            proxy.view.setSelectedRange(NSRange(location: 0, length: 0))
            // Production InputController receives mouse/key events directly;
            // a polled IMK coordinate alone is no longer a takeover signal.
            session.interrupt("鼠标操作已接管本句", commitIfOwned: false)
            session.checkSelection()
            check(!session.awaitingReadback && session.phase == .interrupted, "explicit mouse takeover ignored")
        }
    }
}

test("Every client streams past stale text and relative selection, including both logged VS Code failures") {
    let original = String(repeating: "原", count: 141)
    for bundle in ["com.microsoft.VSCode", "com.microsoft.VSCodeInsiders", "com.openai.codex", "com.tencent.xinWeChat", "test.strict.imk", "unknown.editor"] {
        for caret in [0, 22, 141] {
            for cancel in [false, true] {
                let proxy = StrictIMKClient(original, selection: NSRange(location: caret, length: 0))
                proxy.bundle = bundle
                proxy.staleCompositionRead = true
                proxy.relativeCompositionSelection = true
                let adapter = NativeInputClient(proxy)
                let session = try CompositionSession(client: adapter, utteranceID: "default-stream", sequence: 1)
                let reads = proxy.reads
                for (index, text) in ["今", "今天", "今天下午😀\n开会", "明天下午😀\n开会"].enumerated() {
                    proxy.cachedSelectionOrigin = index < 2 ? 0 : 6
                    session.update(text, final: false)
                    session.checkSelection()
                    let expected = (original as NSString).replacingCharacters(in: NSRange(location: caret, length: 0), with: text)
                    check(session.phase == .listening && !session.awaitingReadback && proxy.view.string == expected,
                          "\(bundle) stalled at caret \(caret): \(session.error)")
                }
                check(proxy.reads == reads, "\(bundle) queried the document cache for a partial ACK")
                if cancel { session.cancel() }
                else { session.update("明天下午😀\n开会。", final: true) }
                let expected = cancel ? original : (original as NSString).replacingCharacters(in: NSRange(location: caret, length: 0), with: "明天下午😀\n开会。")
                check(!session.awaitingReadback && !proxy.view.hasMarkedText() && proxy.view.string == expected,
                      "\(bundle) failed to finish or cancel")
                check(cancel ? session.phase == .undone : session.phase == .dictated, "wrong final phase")
            }
        }
    }
}

test("Default geometry ACK still rejects shifted marks and a real caret move") {
    for wrongRange in [true, false] {
        let proxy = StrictIMKClient("原文")
        proxy.staleCompositionRead = true
        let adapter = NativeInputClient(proxy)
        var now: TimeInterval = 0
        let session = try CompositionSession(client: adapter, utteranceID: "default-guard", sequence: 1, clock: { now })
        proxy.wrongMarkedRange = wrongRange
        session.update("今天", final: false)
        if wrongRange {
            now = 1.1; session.advanceReadback()
            check(session.phase == .error, "wrong marked range granted ownership")
        } else {
            proxy.view.setSelectedRange(NSRange(location: 0, length: 0))
            // Production InputController receives mouse/key events directly;
            // a polled IMK coordinate alone is no longer a takeover signal.
            session.interrupt("鼠标操作已接管本句", commitIfOwned: false)
            session.checkSelection()
            check(!session.awaitingReadback && session.phase == .interrupted, "explicit mouse takeover ignored")
        }
    }
}

test("Stale or unavailable selection never delays otherwise confirmed partials") {
    for selection in [NSRange(location: 2, length: 0), unspecifiedRange] {
        let proxy = StrictIMKClient("原文")
        let adapter = NativeInputClient(proxy)
        var now: TimeInterval = 0
        let session = try CompositionSession(client: adapter, utteranceID: "selection-jitter", sequence: 1, clock: { now })
        let reads = proxy.reads
        session.update("今天", final: false)
        session.checkSelection()
        check(!session.awaitingReadback && proxy.markCount == 1, "healthy polling added a wait")
        proxy.reportedSelections = [selection, selection]
        session.checkSelection()
        check(session.active && !session.awaitingReadback, "old caret blocked live composition")
        session.update("今天下午", final: false)
        session.update("今天下午开会", final: false)
        now = 0.75; session.advanceReadback()
        check(session.phase == .listening && !session.awaitingReadback && proxy.markCount == 3,
              "caret-only uncertainty delayed a partial")
        check(proxy.view.string == "原文今天下午开会" && proxy.reads == reads,
              "resumption duplicated text or queried cached text for polling")
        session.update("今天下午开会。", final: true)
        check(session.phase == .dictated && !proxy.view.hasMarkedText(), "final blocked after transient polling mismatch")
    }
}

test("Delayed marked range is retried beyond half a second without repeating a write") {
    let proxy = StrictIMKClient("原文")
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "marked-jitter", sequence: 1, clock: { now })
    session.update("今", final: false)
    proxy.reportedMarkedRanges = Array(repeating: NSRange(location: 2, length: 1), count: 3)
    session.update("今天", final: false)
    check(session.awaitingReadback && proxy.markCount == 2, "old marked range did not defer confirmation")
    session.update("今天下午😀", final: true)
    now = 0.75; session.advanceReadback()
    check(session.active && session.awaitingReadback && proxy.markCount == 2, "mark was replayed or timed out too soon")
    now = 0.9; session.advanceReadback()
    check(session.phase == .dictated && !session.awaitingReadback && proxy.markCount == 3 && proxy.insertCount == 1,
          "final did not resume once after range recovery")
    check(proxy.view.string == "原文今天下午😀", "range recovery lost or duplicated content")
}

test("A marked range regression after confirmation is transient even with an unchanged caret") {
    let proxy = StrictIMKClient("原文")
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "range-poll", sequence: 1, clock: { now })
    session.update("今天", final: false)
    proxy.reportedMarkedRanges = [unspecifiedRange]
    session.checkSelection()
    check(session.active && session.awaitingReadback, "missing mark was ignored or ended the session immediately")
    now = 0.03; session.advanceReadback()
    check(session.phase == .listening && !session.awaitingReadback && proxy.markCount == 1,
          "one-frame range regression caused a write or a stop")
}

test("Explicit user or focus takeover still ends immediately during coordinate recovery") {
    for reason in ["鼠标操作已接管本句", "键盘输入已接管本句", "已切换输入框"] {
        let proxy = StrictIMKClient("原文")
        let adapter = NativeInputClient(proxy)
        var now: TimeInterval = 0
        let session = try CompositionSession(client: adapter, utteranceID: "explicit-takeover", sequence: 1, clock: { now })
        session.update("今天", final: false)
        proxy.reportedSelections = [NSRange(location: 0, length: 0)]
        session.checkSelection()
        session.update("今天继续听写", final: false)
        session.interrupt(reason)
        check(session.phase == .interrupted && !session.awaitingReadback, "explicit takeover waited for a timeout")
        let body = proxy.view.string
        let writes = proxy.markCount + proxy.insertCount
        now = 0.1; session.advanceReadback()
        session.update("不应出现的迟到结果", final: true)
        check(proxy.view.string == body && proxy.markCount + proxy.insertCount == writes,
              "late coordinates or ASR revived a user-ended session")
    }
}

test("Default geometry ACK does not bypass committed text checks during undo") {
    let proxy = StrictIMKClient("原文")
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "committed-guard", sequence: 1, clock: { now })
    session.update("听写", final: true)
    let writes = proxy.insertCount
    proxy.staleDocumentRead = true
    session.cancel()
    now = 0.6; session.advanceReadback()
    check(proxy.view.string == "原文听写" && proxy.insertCount == writes && proxy.explicitEmptyMarks == 0,
          "committed undo wrote without verifying the sentence")
    check(session.phase != .undone, "unverified undo was reported successful")
}

test("Default geometry ACK does not apply an edit against contradictory committed context") {
    let proxy = StrictIMKClient("明天三点开会")
    proxy.staleCompositionRead = true
    proxy.relativeCompositionSelection = true
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "edit-guard", sequence: 1, clock: { now })
    session.update("改成四点", final: false)
    session.convert()
    session.update("改成四点", final: true)
    check(session.phase == .editing, "stale live cache still blocked editing")
    proxy.staleDocumentRead = true
    session.applyEdit(text: "明天四点开会", revision: session.revision, error: nil)
    now = 0.6; session.advanceReadback()
    check(session.phase != .edited && proxy.explicitNonemptyMarks == 0,
          "model result bypassed committed-context verification")
    check(proxy.view.string == "明天三点开会" || proxy.view.string == "明天三点开会改成四点",
          "unverified model result replaced the original")
}

test("Production adapter undo replaces the exact committed range without creating preedit") {
    let proxy = StrictIMKClient("前😀后", selection: NSRange(location: 3, length: 0))
    let adapter = NativeInputClient(proxy)
    let session = try CompositionSession(client: adapter, utteranceID: "undo", sequence: 1)
    session.update("听写", final: true)
    check(session.canCancel && session.phase == .dictated, "local undo was disabled")
    session.cancel()
    check(proxy.view.string == "前😀后" && session.phase == .undone && !proxy.view.hasMarkedText(),
          "committed undo still failed through production adapter")
    check(proxy.explicitEmptyMarks == 0 && proxy.emptyInserts == 1, "undo used marked-text cancellation")
    let next = try CompositionSession(client: adapter, utteranceID: "next", sequence: 1)
    next.update("下一句", final: true)
    check(proxy.view.string == "前😀下一句后" && next.phase == .dictated, "undo left a stale composition")
}

test("Production adapter conversion removes instruction, edits original and cancels back to dictation") {
    for apply in [false, true] {
        let proxy = StrictIMKClient("明天三点开会")
        let adapter = NativeInputClient(proxy)
        let session = try CompositionSession(client: adapter, utteranceID: "edit", sequence: 1)
        var calls = 0
        session.onEdit = { original, instruction, _ in
            check(original == "明天三点开会" && instruction == "把三点改成四点", "wrong model inputs")
            check(proxy.view.string == original + instruction && proxy.view.hasMarkedText(), "instruction disappeared before model result")
            calls += 1
        }
        session.update("把三点改", final: false)
        session.convert()
        session.update("把三点改成四点", final: true)
        check(calls == 1 && session.phase == .editing, "strict IMK bridge could not start editing")
        let revision = session.revision
        if apply { session.applyEdit(text: "明天四点开会", revision: revision, error: nil) }
        session.cancel()
        check(proxy.view.string == "明天三点开会把三点改成四点" && session.phase == .dictated, "cancel lost dictated sentence")
        session.applyEdit(text: "迟到", revision: revision, error: nil)
        session.cancel()
        check(proxy.view.string == "明天三点开会" && session.phase == .undone, "second cancel did not restore original")
    }
}

test("A swallowed explicit deletion fails once and records the real desired caret") {
    let proxy = StrictIMKClient("已有文字")
    proxy.acceptsEmptyInsert = false
    proxy.ignoreEmptyMark = true
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "failure", sequence: 1, clock: { now })
    session.update("听写", final: true)
    session.cancel()
    check(session.awaitingReadback, "ignored deletion was reported successful")
    now = 0.6
    session.advanceReadback()
    check(session.phase == .error && proxy.emptyInserts == 1 && proxy.explicitEmptyMarks == 0 && proxy.view.string == "已有文字听写", "failed deletion repeated or lost text")
    check(session.readbackDiagnostics["expected_selection"] as? [Int] == [4, 0] &&
          session.readbackDiagnostics["observed_selection"] as? [Int] == [6, 0], "diagnostics hid failed caret movement")
}

test("Context reader accepts bounded windows and rejects inconsistent range evidence") {
    let selection = NSRange(location: 100, length: 0)
    var queries = 0
    let context = EditorContextReader.read(around: selection) { range in
        queries += 1
        let actual = NSIntersectionRange(range, NSRange(location: 90, length: 20))
        guard actual.length > 0 else { return nil }
        return EditorTextRange(text: String(repeating: "字", count: actual.length), range: actual)
    }
    check(context?.range == NSRange(location: 90, length: 20) && queries == 1, "clipped window lost its actual range")
    for bad in [EditorTextRange(text: "x", range: NSRange(location: 99, length: 5)),
                EditorTextRange(text: "xx", range: NSRange(location: 0, length: 2)),
                EditorTextRange(text: "", range: unspecifiedRange)] {
        queries = 0
        let invalid = EditorContextReader.read(around: selection) { _ in queries += 1; return bad }
        check(invalid == nil && queries <= 20, "bad metadata granted context or caused excessive reads")
    }
}

test("Unknown or invalid context never becomes an empty document") {
    var queries = 0
    for selection in [NSRange(location: 0, length: 0), NSRange(location: 200, length: 0)] {
        let missing = EditorContextReader.read(around: selection) { _ in queries += 1; return nil }
        check(missing == nil, "nil was treated as an empty document")
    }
    let previous = queries
    for selection in [unspecifiedRange, NSRange(location: Int.max - 1, length: 1), NSRange(location: 0, length: 100_000)] {
        check(EditorContextReader.read(around: selection) { _ in queries += 1; return nil } == nil,
              "invalid range was accepted")
    }
    check(previous == queries, "invalid ranges reached the client")
}

test("Deletion delivery never falls back to an unspecified range or repeats a write") {
    let range = NSRange(location: 89, length: 6)
    var insertedRanges: [NSRange] = []
    CommittedReplacementDelivery.send("", range: range, insert: { _, actual in insertedRanges.append(actual) })
    CommittedReplacementDelivery.send("", range: NSRange(location: 89, length: 0), insert: { _, actual in insertedRanges.append(actual) })
    CommittedReplacementDelivery.send("", range: unspecifiedRange, insert: { _, actual in insertedRanges.append(actual) })
    check(insertedRanges == [range], "empty deletion wrote at an unknown range or repeated an operation")
}

final class WeChatFixture {
    let proxy: StrictIMKClient
    let adapter: NativeInputClient
    var now: TimeInterval = 0
    var keys: [String] = []
    var undoText = ""
    var keyError: String?
    var dropSelectAll = false
    var dropSentenceSelection = false
    var dropCaretMovement = false
    var reportedEndOffset = 0
    init(_ text: String, caret: Int? = nil, codex: Bool = false, application: String? = nil, directRange: Bool = true) {
        proxy = StrictIMKClient(text, selection: caret.map { NSRange(location: $0, length: 0) })
        proxy.ignoreReplacementRange = !codex
        if codex {
            proxy.bundle = "com.openai.codex"
            proxy.preserveCommittedCaret = true
            proxy.ignoreMarkedReplacementRange = true
            proxy.acceptsEmptyInsert = true
        }
        proxy.bundle = application ?? (codex ? "com.openai.codex" : "com.tencent.xinWeChat")
        adapter = NativeInputClient(proxy)
        let deliver: WeChatCompatibility.SendKey = { [unowned self] command, count, _, done in
            keys.append(command)
            if let keyError { done(keyError); return }
            switch command {
            case "select_to_start":
                proxy.view.moveToBeginningOfDocumentAndModifySelection(nil)
            case "select_all":
                if !dropSelectAll { proxy.view.setSelectedRange(NSRange(location: 0, length: (proxy.view.string as NSString).length)) }
            case "undo":
                proxy.view.string = undoText
                proxy.view.setSelectedRange(NSRange(location: (undoText as NSString).length, length: 0))
            case "select_previous":
                if !dropSentenceSelection {
                    for _ in 0..<count { proxy.view.moveLeftAndModifySelection(nil) }
                }
            case "delete":
                proxy.view.deleteBackward(nil)
            case "caret_from_end":
                let prefix = String(proxy.view.string.dropLast(count))
                proxy.view.setSelectedRange(NSRange(location: (prefix as NSString).length, length: 0))
            case "caret_to_end":
                if !dropCaretMovement { proxy.view.moveToEndOfDocument(nil) }
                if reportedEndOffset != 0 {
                    proxy.reportedSelections = [NSRange(location: proxy.view.selectedRange().location + reportedEndOffset, length: 0)]
                }
            case "caret_backward":
                if !dropCaretMovement { for _ in 0..<count { proxy.view.moveLeft(nil) } }
            case "caret_forward":
                if !dropCaretMovement { for _ in 0..<count { proxy.view.moveRight(nil) } }
            default: fatalError("unexpected command")
            }
            done(nil)
        }
        adapter.configureKeyDelivery(deliver, clock: { [unowned self] in now })
        // Exercise the former direct-range strategy independently. Production
        // factory cases below use directRange:false and native selection.
        if codex && directRange {
            adapter.nativeCompatibility = WeChatCompatibility(client: adapter, sendKey: deliver, clock: { [unowned self] in now })
        }
    }
    func session() throws -> CompositionSession {
        try CompositionSession(client: adapter, utteranceID: "wechat", sequence: 1, clock: { [unowned self] in now })
    }
    func drain(_ session: CompositionSession) {
        for _ in 0..<60 {
            now += 0.06
            adapter.advanceClientOperation()
            session.advanceReadback()
            if !adapter.clientOperationPending && !session.awaitingReadback { return }
        }
        fatalError("operation hung")
    }
}

test("Default client configuration selects before replacement in every application") {
    for bundle in ["com.openai.codex", "com.microsoft.VSCode", "com.apple.TextEdit", "com.apple.Safari", "org.example.Editor"] {
        let f = WeChatFixture("前😀原文后缀", caret: 3, codex: true, application: bundle, directRange: false)
        check(f.adapter.nativeCompatibility != nil && f.adapter.compatibility == nil,
              "default client did not receive native key operations")
        let s = try f.session()
        s.update("扩写", final: false); s.convert(); s.update("扩写", final: true)
        s.applyEdit(text: "扩写后的完整内容😀\n末尾。", revision: s.revision, error: nil); f.drain(s)
        check(s.phase == .edited && f.proxy.view.selectedRange().location == (f.proxy.view.string as NSString).length,
              "default replacement did not recover the retained old caret")
        s.cancel(); f.drain(s)
        check(s.phase == .dictated && f.proxy.view.string == "前😀扩写原文后缀" && f.proxy.view.selectedRange().location == 5,
              "default edit undo did not restore the middle insertion point")
        s.cancel(); f.drain(s)
        check(s.phase == .undone && f.proxy.view.string == "前😀原文后缀", "default sentence undo failed")
        check(f.keys.contains("select_previous") && f.keys.contains("delete") && f.keys.contains("select_all") &&
              !f.keys.contains("copy") && f.proxy.explicitEmptyMarks == 0,
              "default flow regressed to whole-field selection or empty marked replacement")
        let next = try f.session(); next.update("继续", final: true)
        check(next.phase == .dictated && f.proxy.view.string == "前😀继续原文后缀", "next sentence did not resume after undo")
    }
}

test("A default client's dropped sentence-selection key never deletes neighbouring text") {
    let f = WeChatFixture("原文", codex: true, application: "org.example.Editor")
    let s = try f.session(); s.update("听写", final: true)
    f.dropSentenceSelection = true
    s.cancel(); f.drain(s)
    check(s.phase == .error && f.proxy.view.string == "原文听写" && f.keys == ["select_previous"],
          "default undo deleted an unconfirmed range or repeated keys")
}

test("Negative control reproduces Codex appending an explicit marked-text replacement") {
    let f = WeChatFixture(String(repeating: "原", count: 67), caret: 65, codex: true)
    f.proxy.setMarkedText(String(repeating: "新", count: 55), selectionRange: NSRange(location: 55, length: 0),
                          replacementRange: NSRange(location: 0, length: 67))
    check((f.proxy.view.string as NSString).length == 122 && f.proxy.view.markedRange() == NSRange(location: 65, length: 55),
          "logged Codex marked replacement failure was not reproduced")
}

test("Codex edits write once without marked replacement and continue at the new end") {
    let cases: [(Int, Int, String)] = [
        (14, 14, String(repeating: "扩", count: 32)), (14, 14, "短"),
        (14, 14, "第一行😀\n第二行👨‍👩‍👧‍👦结束"),
        (67, 65, String(repeating: "新", count: 55)),
        (67, 65, String(repeating: "扩", count: 131)),
        (14, 7, String(repeating: "长", count: 2000))]
    for (size, caret, result) in cases {
        let original = String(repeating: "原", count: size)
        let f = WeChatFixture(original, caret: caret, codex: true)
        let s = try f.session()
        s.update("修改原文", final: false); s.convert(); s.update("修改原文", final: true)
        let writes = f.proxy.insertCount
        s.applyEdit(text: result, revision: s.revision, error: nil); f.drain(s)
        check(s.phase == .edited && f.proxy.view.string == result,
              "Codex edit failed: size=\(size) caret=\(caret) result=\((result as NSString).length) phase=\(s.phase) error=\(s.error) selection=\(f.proxy.view.selectedRange()) keys=\(f.keys) body=\((f.proxy.view.string as NSString).length) diagnostics=\(s.readbackDiagnostics)")
        check(f.proxy.insertCount == writes + 2 && f.proxy.explicitNonemptyMarks == 0 && !f.proxy.view.hasMarkedText(),
              "result was rewritten or left in composition")
        check(f.proxy.view.selectedRange() == NSRange(location: (result as NSString).length, length: 0), "caret not at result end")
        check(f.keys.allSatisfy { $0 == "caret_to_end" } && f.keys.count <= 1, "caret positioning used full selection or repeated keys")
        let record = s.undoRecord()!
        let next = try f.session()
        next.update("下一句", final: true)
        check(f.proxy.view.string == result + "下一句", "next dictation inserted in the middle")
        next.cancel(); f.drain(next)
        check(next.phase == .undone && f.proxy.view.string == result, "next dictation undo failed")
        let restored = CompositionSession(client: f.adapter, undo: record, utteranceID: "history", sequence: 2, revision: 2,
                                          clock: { f.now })
        restored.cancel(); f.drain(restored)
        let dictated = (original as NSString).replacingCharacters(in: NSRange(location: caret, length: 0), with: "修改原文")
        check(restored.phase == .dictated && f.proxy.view.string == dictated, "historical edit restore failed")
        check(f.proxy.explicitNonemptyMarks == 0 && !f.keys.contains("select_all"), "restore used marked/full-selection replacement")
        restored.cancel(); f.drain(restored)
        check(restored.phase == .undone && f.proxy.view.string == original, "middle instruction could not be undone after restore")
    }
}

test("Codex mixed history continues after an edit restore receives the stale pre-replacement caret") {
    let f = WeChatFixture("\n\n", caret: 0, codex: true)
    let history = CompositionUndoHistory(); history.configure(enabled: true)
    for raw in ["一二三四五六", "一二三四五六七", "一二三四五六七八"] {
        let s = try f.session(); history.begin(s.original)
        s.update(raw, final: true); history.settled(s)
    }
    let edit = try f.session(); history.begin(edit.original)
    edit.update("一二三四五六", final: false); edit.convert(); edit.update("一二三四五六", final: true)
    edit.applyEdit(text: String(repeating: "新", count: 31), revision: edit.revision, error: nil); f.drain(edit)
    history.settled(edit)
    let last = try f.session(); history.begin(last.original)
    last.update("下一句话", final: true); last.cancel(); f.drain(last)
    let restored = CompositionSession(client: f.adapter, undo: history.pop()!, utteranceID: "history",
                                      sequence: 2, revision: 2, clock: { f.now })
    // Real log: restored text is 29 UTF-16 units, target 27, cached caret 31.
    // Cmd+Down actually reaches 29; the stale 31 causes four Left keys -> 25.
    f.reportedEndOffset = 2; f.proxy.cachedParagraphSuffix = 2
    let writes = f.proxy.insertCount
    let started = f.now
    restored.cancel(); f.drain(restored)
    check(restored.phase == .dictated && f.proxy.view.selectedRange() == NSRange(location: 27, length: 0),
          "stale caret broke historical edit restore: \(restored.error), \(f.keys)")
    check(f.proxy.insertCount == writes + 1 && f.keys.suffix(3) == ["caret_to_end", "caret_backward", "caret_forward"],
          "caret recovery rewrote text or did not correct the overshoot")
    check(f.now - started < 0.3, "corrected restore waited for the error timeout")
    f.reportedEndOffset = 0; f.proxy.cachedParagraphSuffix = 0
    restored.cancel(); f.drain(restored)
    check(restored.phase == .undone, "restored edit instruction could not be removed")
    while let record = history.pop() {
        let prior = CompositionSession(client: f.adapter, undo: record, utteranceID: "history",
                                       sequence: 2, revision: 4, clock: { f.now })
        prior.cancel(); f.drain(prior)
        check(prior.phase == .undone, "older dictation failed after edit undo")
    }
    check(f.proxy.view.string == "\n\n" && f.proxy.view.selectedRange() == NSRange(location: 0, length: 0),
          "mixed history did not return to its original body and caret")
    check(!f.keys.contains("select_all"), "history recovery selected the whole field")
}

test("Codex restore does not repeat a move while selection readback is unchanged") {
    let f = WeChatFixture("旧结果", codex: true)
    f.adapter.replaceEditing(NSRange(location: 0, length: 3), with: "原文指令后缀", restoringCaret: 4)
    f.adapter.advanceClientOperation() // Native end at 6.
    f.now += 0.06; f.adapter.advanceClientOperation() // Two Left keys -> 4.
    f.proxy.reportedSelections = Array(repeating: NSRange(location: 6, length: 0), count: 3)
    for _ in 0..<3 { f.now += 0.06; f.adapter.advanceClientOperation() }
    check(f.keys == ["caret_to_end", "caret_backward"] && f.proxy.view.selectedRange().location == 4,
          "stale selection repeated an already-delivered motion")
    f.now += 0.06; f.adapter.advanceClientOperation()
    check(!f.adapter.clientOperationPending && f.adapter.clientOperationFailure == nil,
          "fresh selection did not finish the restore")
}

test("Codex caret waits for result text and never resends a replacement") {
    let f = WeChatFixture("原文", codex: true)
    f.adapter.replace(NSRange(location: 0, length: 2), with: "确认后的编辑结果")
    f.proxy.staleDocumentRead = true
    f.now += 0.1; f.adapter.advanceClientOperation()
    check(f.keys.isEmpty && f.adapter.clientOperationPending, "moved caret before result text was confirmed")
    f.proxy.staleDocumentRead = false
    f.adapter.advanceClientOperation(); f.now += 0.1; f.adapter.advanceClientOperation()
    check(f.keys == ["caret_to_end"] && f.proxy.insertCount == 1 && !f.adapter.clientOperationPending,
          "delayed acknowledgement repeated text or caret motion")
}

test("Codex bounded replacement also puts the caret at the full document end without changing its suffix") {
    let f = WeChatFixture("前原文后文", caret: 1, codex: true)
    f.adapter.replace(NSRange(location: 1, length: 2), with: "扩😀写")
    f.adapter.advanceClientOperation(); f.now += 0.1; f.adapter.advanceClientOperation()
    check(f.proxy.view.string == "前扩😀写后文" && f.proxy.view.selectedRange() == NSRange(location: 7, length: 0),
          "bounded edit did not reach full document end or changed the suffix")
    check(f.keys == ["caret_to_end"] && !f.adapter.clientOperationPending, "bounded edit did not use a single end command")
}

test("Codex independently delayed caret acknowledgement is observed without extra writes or keys") {
    let f = WeChatFixture("原文", codex: true)
    f.dropCaretMovement = true
    f.adapter.replace(NSRange(location: 0, length: 2), with: "恢复后的听写结果")
    f.proxy.view.setSelectedRange(NSRange(location: 1, length: 0))
    f.adapter.advanceClientOperation()
    check(f.adapter.clientOperationPending && f.adapter.clientOperationFailure == nil && f.keys == ["caret_to_end"],
          "interim selection blocked the end command")
    f.now += 0.1
    f.proxy.view.setSelectedRange(NSRange(location: 8, length: 0))
    f.adapter.advanceClientOperation()
    check(!f.adapter.clientOperationPending && f.adapter.clientOperationFailure == nil && f.keys == ["caret_to_end"] && f.proxy.insertCount == 1,
          "late correct caret did not finish without another write")
}

test("Codex visual end is accepted when IMK exposes a trailing paragraph sentinel") {
    let f = WeChatFixture("原文", codex: true)
    f.proxy.trailingParagraphSentinel = true
    f.adapter.replace(NSRange(location: 0, length: 2), with: "确认的结果")
    f.adapter.advanceClientOperation(); f.now += 0.1; f.adapter.advanceClientOperation()
    check(f.keys == ["caret_to_end"] && !f.adapter.clientOperationPending && f.adapter.clientOperationFailure == nil,
          "already-correct caret waited on an irrelevant EOF probe")
    check(f.adapter.verifiedReplacementCaret == NSRange(location: 5, length: 0) && f.proxy.insertCount == 1,
          "visual end was not acknowledged or result was rewritten")
}

test("Codex leaves editing immediately after caret delivery without waiting for EOF timeout") {
    let f = WeChatFixture("原文", codex: true)
    let s = try f.session()
    s.update("扩写", final: false); s.convert(); s.update("扩写", final: true)
    f.proxy.trailingParagraphSentinel = true
    s.applyEdit(text: "已经替换的结果", revision: s.revision, error: nil)
    f.drain(s)
    check(s.phase == .edited && !s.awaitingReadback && s.error.isEmpty && f.now <= 0.15,
          "loading state persisted after text and caret were confirmed")
}

test("Codex dropped caret motion fails without duplicate text or a pending composition") {
    let f = WeChatFixture("原文", codex: true)
    f.dropCaretMovement = true
    f.adapter.replace(NSRange(location: 0, length: 2), with: "编辑后的完整结果")
    f.adapter.advanceClientOperation()
    for _ in 0..<30 { f.now += 0.1; f.adapter.advanceClientOperation() }
    check(f.proxy.view.string == "编辑后的完整结果" && f.proxy.insertCount == 1 && f.proxy.explicitNonemptyMarks == 0,
          "failed caret movement rewrote text")
    check(f.keys == ["caret_to_end"] && f.adapter.clientOperationFailure != nil && !f.adapter.clientOperationPending,
          "failed movement was accepted or retried")
}

test("Codex controller takeover or cancellation prevents late movement") {
    for cancel in [true, false] {
        let f = WeChatFixture("原文", codex: true)
        if cancel { f.proxy.onInsert = { f.adapter.cancelClientOperation() } }
        f.adapter.replace(NSRange(location: 0, length: 2), with: "扩写结果")
        if !cancel {
            f.proxy.view.setSelectedRange(NSRange(location: 1, length: 0))
            f.adapter.cancelClientOperation()
        }
        f.adapter.advanceClientOperation(); f.now += 3; f.adapter.advanceClientOperation()
        check(f.keys.isEmpty && f.proxy.insertCount == 1 && !f.adapter.clientOperationPending, "takeover moved caret or wrote again")
    }
}

test("Codex caret commands are admitted only while the exact native motion is pending") {
    let f = WeChatFixture("原文", codex: true)
    f.dropCaretMovement = true
    f.adapter.replace(NSRange(location: 0, length: 2), with: "扩写的结果")
    f.adapter.advanceClientOperation()
    let helper = f.adapter.nativeCompatibility!
    check(!helper.acceptsKey(code: 0, command: true, otherModifiers: false, tag: 0), "accepted select-all")
    check(helper.acceptsKey(code: 125, command: true, otherModifiers: false, tag: 0), "native end key was mistaken for manual takeover")
    check(!helper.acceptsKey(code: 125, command: true, otherModifiers: false, tag: 0), "admitted repeated key")
}

test("Negative control reproduces Qt ignoring a nonempty replacement range") {
    let f = WeChatFixture("今天三点开会")
    f.proxy.insertText("今天四点开会", replacementRange: NSRange(location: 0, length: 6))
    check(f.proxy.view.string == "今天三点开会今天四点开会", "ignored replacement was not reproduced")
}

test("WeChat edits original full text and cancel restores dictation then undoes only that sentence") {
    let f = WeChatFixture("今天三点开会")
    let s = try f.session()
    var modelOriginal = ""
    s.onEdit = { original, instruction, _ in
        modelOriginal = original
        check(instruction == "把三点改成四点", "instruction changed")
    }
    s.update("把三点改成四点", final: false); s.convert(); s.update("把三点改成四点", final: true)
    f.drain(s)
    check(s.phase == .editing && modelOriginal == "今天三点开会", "full original was not captured")
    check(f.keys.isEmpty && f.proxy.view.hasMarkedText() && f.proxy.view.string == "今天三点开会把三点改成四点", "WeChat selected or removed text before model result")
    s.applyEdit(text: "今天四点开会", revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .edited && f.proxy.view.string == "今天四点开会", "edit appended or failed")
    check(!f.keys.contains("caret_from_end"), "already correct caret caused an unnecessary key")
    s.cancel(); f.drain(s)
    check(s.phase == .dictated && f.proxy.view.string == "今天三点开会把三点改成四点", "restore did not recover dictation")
    s.cancel(); f.drain(s)
    check(s.phase == .undone && f.proxy.view.string == "今天三点开会", "second cancel lost original")
    check(!f.keys.contains("undo"), "restored edit must not trust native undo history")
}

test("WeChat committed undo selects only the sentence and uses one Backspace regardless of undo history") {
    for nativeResult in ["原文", "原文听", ""] {
        let f = WeChatFixture("原文")
        f.undoText = nativeResult
        let s = try f.session()
        s.update("听写", final: true)
        s.cancel(); f.drain(s)
        check(s.phase == .undone && f.proxy.view.string == "原文", "native undo grouping damaged original")
        check(f.keys == ["select_previous", "delete"], "ordinary undo selected all or depended on native undo")
    }
}

test("WeChat live cancellation clears only the IME composition without any shortcut") {
    let f = WeChatFixture("原文")
    let s = try f.session()
    s.update("未定稿", final: false); s.cancel(); f.drain(s)
    check(s.phase == .undone && f.proxy.view.string == "原文" && f.keys.isEmpty,
          "live undo selected or deleted committed text")
}

test("WeChat sentence undo handles emoji, suffix and original replaced selection") {
    let f = WeChatFixture("前原文后", caret: 3)
    f.proxy.view.setSelectedRange(NSRange(location: 1, length: 2))
    let s = try f.session()
    s.update("👨‍👩‍👧‍👦听写", final: true); s.cancel(); f.drain(s)
    check(s.phase == .undone && f.proxy.view.string == "前原文后", "selection restore lost text")
    check(f.keys == ["select_previous"], "restoring a selection used whole-field commands")
}

test("A dropped sentence selection never sends Backspace") {
    let f = WeChatFixture("原文")
    let s = try f.session()
    s.update("听写", final: true)
    f.dropSentenceSelection = true
    s.cancel(); f.drain(s)
    check(s.phase == .error && f.proxy.view.string == "原文听写" && f.keys == ["select_previous"],
          "unconfirmed sentence selection deleted text")
}

test("WeChat undo preserves suffix and UTF16 caret with emoji") {
    let f = WeChatFixture("前😀后文", caret: 3)
    f.undoText = "前😀后文"
    let s = try f.session()
    s.update("新增", final: true); s.cancel(); f.drain(s)
    check(s.phase == .undone && f.proxy.view.string == "前😀后文", "suffix lost")
    check(f.proxy.view.selectedRange() == NSRange(location: 3, length: 0), "UTF16 caret changed")
}

test("WeChat empty result and original empty document do not leave a composition") {
    let f = WeChatFixture("原文")
    let s = try f.session()
    s.update("删除全文", final: false); s.convert(); s.update("删除全文", final: true); f.drain(s)
    s.applyEdit(text: "", revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .edited && f.proxy.view.string.isEmpty && !f.proxy.view.hasMarkedText(), "empty result failed")
    s.cancel(); f.drain(s)
    check(s.phase == .dictated && f.proxy.view.string == "原文删除全文", "empty result cannot restore")
}

test("WeChat denied keys do not append model result") {
    let f = WeChatFixture("原文")
    let s = try f.session()
    s.update("修改", final: false); s.convert(); s.update("修改", final: true); f.drain(s)
    f.keyError = "permission denied"
    s.applyEdit(text: "新文", revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .error && f.proxy.view.string == "原文", "permission failure wrote result")
}

test("WeChat takeover invalidates pending compatibility work") {
    let f = WeChatFixture("原文")
    let s = try f.session()
    s.update("修改", final: false); s.convert(); s.update("修改", final: true); f.drain(s)
    s.applyEdit(text: "新文", revision: s.revision, error: nil)
    s.interrupt("user typed", commitIfOwned: false)
    f.proxy.view.string = "用户新输入"
    f.drain(s)
    check(s.phase == .interrupted && f.proxy.view.string == "用户新输入", "late replacement overwrote takeover")
}

test("Dropped select-all cannot mistake a prefix for the full document") {
    let f = WeChatFixture("原文后文")
    f.dropSelectAll = true
    f.proxy.view.setSelectedRange(NSRange(location: 0, length: 2))
    f.adapter.compatibility!.replace(NSRange(location: 0, length: 2), with: "新文", undo: false)
    for _ in 0..<50 { f.now += 0.06; f.adapter.compatibility!.advance() }
    check(f.adapter.clientOperationFailure != nil && f.proxy.view.string == "原文后文", "prefix selected was overwritten")
}

test("Qt missing key tags admit only the pending exact native command once") {
    let f = WeChatFixture("原文")
    let c = f.adapter.compatibility!
    c.replace(NSRange(location: 0, length: 2), with: "新文", undo: false)
    check(!c.acceptsKey(code: 6, command: true, otherModifiers: false, tag: 0), "unrequested undo admitted")
    check(!c.acceptsKey(code: 0, command: true, otherModifiers: true, tag: 0), "different modifier admitted")
    check(!c.acceptsKey(code: 0, command: true, otherModifiers: false, tag: c.eventTag + 1), "wrong nonzero tag admitted")
    check(c.acceptsKey(code: 0, command: true, otherModifiers: false, tag: 0), "stripped native select-all rejected")
    check(!c.acceptsKey(code: 0, command: true, otherModifiers: false, tag: 0), "replayed select-all admitted")
    c.replace(NSRange(location: 0, length: 2), with: "新文", undo: false)
    f.now += 0.6
    check(!c.acceptsKey(code: 0, command: true, otherModifiers: false, tag: 0), "late manual command admitted")
    c.cancel()
    check(!c.acceptsKey(code: 0, command: true, otherModifiers: false, tag: c.eventTag), "cancelled key admitted")
}

test("WeChat and Codex historical dictation undo selects only each sentence then Backspace") {
    for codex in [false, true] {
        let f = WeChatFixture("原文")
        if codex {
            f.proxy.bundle = "com.openai.codex"
            f.proxy.ignoreReplacementRange = false
            f.adapter.nativeCompatibility = f.adapter.compatibility
            f.adapter.compatibility = nil
        }
        let history = CompositionUndoHistory()
        history.configure(enabled: true)
        let first = try f.session(); first.update("甲😀", final: true); history.settled(first)
        let second = try f.session(); history.begin(second.original); second.update("乙", final: true)
        second.cancel(); f.drain(second)
        check(second.phase == .undone && f.proxy.view.string == "原文甲😀", "current undo failed")
        let restored = CompositionSession(client: f.adapter, undo: history.pop()!, utteranceID: "history",
                                         sequence: 2, revision: 3, clock: { f.now })
        restored.cancel(); f.drain(restored)
        check(restored.phase == .undone && f.proxy.view.string == "原文", "historical undo failed")
        check(f.keys == ["select_previous", "delete", "select_previous", "delete"], "ordinary undo selected whole field")
    }
}

test("Historical edit restores dictation and supports deleting it with both adapters") {
    for codex in [false, true] {
        let f = WeChatFixture("原文")
        if codex {
            f.proxy.bundle = "com.openai.codex"
            f.proxy.ignoreReplacementRange = false
            f.adapter.nativeCompatibility = f.adapter.compatibility
            f.adapter.compatibility = nil
        }
        let s = try f.session()
        s.update("改为新文", final: false); s.convert(); s.update("改为新文", final: true)
        s.applyEdit(text: "新文", revision: s.revision, error: nil); f.drain(s)
        check(s.phase == .edited, "edit precondition failed")
        let record = s.undoRecord()!
        let later = try f.session(); later.update("后一句", final: true)
        later.cancel(); f.drain(later)
        let restored = CompositionSession(client: f.adapter, undo: record, utteranceID: "history-edit",
                                         sequence: 1, revision: 10, clock: { f.now })
        restored.cancel(); f.drain(restored)
        check(restored.phase == .dictated && f.proxy.view.string == "原文改为新文", "edit restore failed")
        restored.cancel(); f.drain(restored)
        check(restored.phase == .undone && f.proxy.view.string == "原文", "restored instruction undo failed")
        if codex { check(!f.keys.contains("select_all"), "Codex edit used WeChat full-field keys") }
    }
}

test("Default selected replacement cannot leave the ignored explicit-range suffix behind") {
    for bundle in ["com.microsoft.VSCode", "com.openai.codex", "org.example.Editor"] {
        let f = WeChatFixture("第一句。最后一句。", codex: true, application: bundle, directRange: false)
        f.proxy.ignoreReplacementRange = true
        for (instruction, result) in [("扩写", "第一句扩写后的内容。第二句是补充。最后一句。"),
                                      ("删掉最后一句话", "第一句扩写后的内容。第二句是补充。"),
                                      ("只保留第一句", "第一句扩写后的内容。") ] {
            let s = try f.session()
            let keysBefore = f.keys
            s.update(instruction, final: false); s.convert(); s.update(instruction, final: true); f.drain(s)
            check(f.keys == keysBefore && f.proxy.view.hasMarkedText(), "model wait changed the visible selection")
            s.applyEdit(text: result, revision: s.revision, error: nil); f.drain(s)
            check(s.phase == .edited && f.proxy.view.string == result && !s.awaitingReadback,
                  "editing retained or reintroduced a previous result")
            check((f.adapter.clientOperationDiagnostics["post_write_ms"] as? Double ?? 999) <= 61,
                  "selected replacement delayed the loading indicator after writing")
            check(!f.proxy.view.hasMarkedText(), "edit left a composition behind")
        }
        check(f.keys == Array(repeating: ["select_to_start", "select_all"], count: 3).flatMap { $0 }, "edits copied text or repeated native selection")
    }
}

test("A missing post-edit document cache does not keep the successful edit loading") {
    let f = WeChatFixture("原文", codex: true, application: "com.microsoft.VSCode", directRange: false)
    let s = try f.session()
    s.update("扩写", final: false); s.convert(); s.update("扩写", final: true); f.drain(s)
    let result = String(repeating: "扩写的结果。", count: 20)
    var readsAtWrite = 0
    f.proxy.onInsert = {
        if f.proxy.view.string == result {
            readsAtWrite = f.proxy.reads
            f.proxy.unavailableDocumentRead = true
        }
    }
    s.applyEdit(text: result, revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .edited && s.error.isEmpty && f.now <= 0.4, "successful result waited for a cache timeout")
    check(f.proxy.reads - readsAtWrite == 1, "adapter and transaction both read back the full result")
    check(f.adapter.verifiedReplacement?.text == result, "missing scoped write receipt")
}

test("Selected replacement still recovers an old caret without re-reading the result") {
    let f = WeChatFixture("原文", codex: true, directRange: false)
    let s = try f.session()
    s.update("扩写", final: false); s.convert(); s.update("扩写", final: true); f.drain(s)
    let result = "已经替换完毕的新内容"
    var readsAtWrite = 0
    f.proxy.onInsert = {
        if f.proxy.view.string == result {
            readsAtWrite = f.proxy.reads
            f.proxy.view.setSelectedRange(NSRange(location: 1, length: 0))
        }
    }
    s.applyEdit(text: result, revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .edited && f.proxy.view.selectedRange().location == (result as NSString).length,
          "selected replacement retained the old caret")
    check(f.proxy.reads - readsAtWrite == 1 && f.keys.suffix(2) == ["select_all", "caret_from_end"],
          "caret recovery repeated document queries or selection")
}

test("Default selection or insertion loss cannot apply or duplicate a model result") {
    for dropSelection in [true, false] {
        let f = WeChatFixture("完整的原文", caret: 2, codex: true, directRange: false)
        let s = try f.session()
        s.update("修改", final: false); s.convert(); s.update("修改", final: true); f.drain(s)
        f.dropSelectAll = dropSelection
        f.proxy.onInsert = { if !dropSelection { f.proxy.dropNonemptyInsert = true } }
        s.applyEdit(text: "模型的结果", revision: s.revision, error: nil); f.drain(s)
        check(s.phase != .edited && f.proxy.view.string == "完整修改的原文", "a dropped operation overwrote original text")
        check(f.keys.filter { $0 == "select_all" }.count == 1, "a dropped operation was blindly retried")
    }
}

test("Default mixed undo history crosses edits and then continues deleting older dictation") {
    let f = WeChatFixture("原文", codex: true, directRange: false)
    let history = CompositionUndoHistory(); history.configure(enabled: true)
    for raw in ["第一句。", "第二句😀。"] {
        let s = try f.session(); history.begin(s.original)
        s.update(raw, final: true); history.settled(s)
    }
    let s = try f.session(); history.begin(s.original)
    s.update("扩写", final: false); s.convert(); s.update("扩写", final: true); f.drain(s)
    s.applyEdit(text: "完整的扩写结果。", revision: s.revision, error: nil); f.drain(s); history.settled(s)
    let next = try f.session(); history.begin(next.original)
    while let record = history.pop() {
        let restored = CompositionSession(client: f.adapter, undo: record, utteranceID: "history",
                                          sequence: 2, revision: 4, clock: { f.now })
        restored.cancel(); f.drain(restored)
        if restored.phase == .dictated { restored.cancel(); f.drain(restored) }
        check(restored.phase == .undone, "undo stack stopped: raw=\(record.raw), phase=\(restored.phase), error=\(restored.error), body=\(f.proxy.view.string), keys=\(f.keys), diag=\(restored.readbackDiagnostics)")
    }
    check(f.proxy.view.string == "原文" && f.keys.filter { $0 == "select_all" }.count == 2,
          "sentence undo selected the full field or old text was duplicated")
}

test("Default selection replacement preserves a bounded context's prefix and suffix") {
    let f = WeChatFixture("前文目标后文", codex: true, directRange: false)
    f.adapter.replaceEditing(NSRange(location: 2, length: 2), with: "修改后的目标", restoringCaret: nil)
    for _ in 0..<3 { f.now += 0.06; f.adapter.advanceClientOperation() }
    check(!f.adapter.clientOperationPending && f.adapter.clientOperationFailure == nil &&
          f.proxy.view.string == "前文修改后的目标后文", "bounded context overwrote the unedited suffix")
    check(f.adapter.verifiedReplacement?.range == NSRange(location: 2, length: 6), "receipt does not match the requested scope")
}

test("Source comparison distinguishes terminal paragraph metadata from changed content") {
    func compare(_ cached: String, _ selected: String, at location: Int = 0) -> EditSourceComparison {
        EditSourceComparison.compare(EditorTextRange(text: cached,
            range: NSRange(location: location, length: (cached as NSString).length)), with: selected)
    }
    check(compare("正文\n\n", "正文") == .terminalParagraphs, "logged two paragraph sentinels rejected")
    check(compare("正文\r\n", "正文") == .terminalParagraphs, "CRLF boundary rejected")
    check(compare("正文\u{2029}", "正文") == .terminalParagraphs, "paragraph separator rejected")
    check(compare("前\n后\n\n", "前\n后\n\n") == .exact, "real blank lines were normalized away")
    for pair in [("前\n后\n\n", "前后"), ("前 后\n\n", "前后"),
                 ("原文\n\n", "变文"), ("正文旧尾文\n\n", "正文"), ("正文 \n\n", "正文"),
                 ("正文", "正文\n"), ("正文\u{200b}\n\n", "正文")] {
        check(compare(pair.0, pair.1) == .different, "real content difference was ignored")
    }
    check(compare("正文\n\n", "正文", at: 4) == .different, "bounded context was treated as whole document")
    check(EditSourceComparison.compare(nil, with: "正文") == .different, "missing source was accepted")
}

test("Logged extra IMK paragraph separators do not reject valid edits in any default client") {
    for bundle in ["com.openai.codex", "com.microsoft.VSCode", "test.generic.editor"] {
        for original in ["12345。12345。", "12345已完成，前期第1点，适配测试通过。", "甲😀\n乙\n\n"] {
            for caret in [1, (original as NSString).length] {
                let f = WeChatFixture(original, caret: caret, codex: true, directRange: false)
                f.proxy.bundle = bundle
                f.proxy.unselectedCachedSuffix = "\n\n"
                let s = try f.session()
                var requests = 0
                s.onEdit = { source, _, _ in
                    requests += 1
                    check(source == original + "\n\n", "fixture did not reproduce cached paragraph suffix")
                }
                s.update("润色一下。", final: false); s.convert(); s.update("润色一下。", final: true)
                let dictated = f.proxy.view.string
                for _ in 0..<120 { f.now += 0.06; f.adapter.advanceClientOperation(); s.advanceReadback() }
                check(requests == 1 && f.keys.isEmpty && f.proxy.view.hasMarkedText(), "waiting selected the field or lost underline")
                let candidate = "修改后的正文😀\n\n"
                s.applyEdit(text: candidate, revision: s.revision, error: nil); f.drain(s)
                check(s.phase == .edited && s.error.isEmpty && f.proxy.view.string == candidate && requests == 1,
                      "terminal paragraph metadata blocked editing or trimmed model output: \(s.error)")
                check(f.keys.filter { $0 == "select_all" }.count == 1, "representation match caused another selection")
                // The undo source must come from the actual native selection,
                // not the longer IMK substring window sent to the model.
                s.cancel(); f.drain(s)
                check(s.phase == .dictated && f.proxy.view.string == dictated, "edit undo reintroduced cached terminal paragraphs")
                s.cancel(); f.drain(s)
                check(s.phase == .undone && f.proxy.view.string == original, "second undo lost original blank lines")
            }
        }
    }
}

test("Post-model source mismatch preserves dictation without a second model request") {
    for caret in [4, 9] {
        let original = "第一句。最后一句。"
        let f = WeChatFixture(original, caret: caret, codex: true, directRange: false)
        f.proxy.unselectedCachedSuffix = "旧扩写结果。这些文字已不在编辑器内。"
        let s = try f.session()
        check(s.editOriginal?.text != original, "fixture lacks stale context")
        var requests = 0
        s.onEdit = { _, _, _ in requests += 1 }
        s.update("删掉最后一句话", final: false); s.convert(); s.update("删掉最后一句话", final: true)
        let oldRevision = s.revision
        let dictated = f.proxy.view.string
        for _ in 0..<120 { f.now += 0.06; f.adapter.advanceClientOperation(); s.advanceReadback() }
        check(requests == 1 && s.phase == .editing && f.keys.isEmpty && f.proxy.view.hasMarkedText(),
              "model wait selected the document, cleared preedit or requested another model")
        s.applyEdit(text: "错误结果带回旧尾文", revision: oldRevision, error: nil); f.drain(s)
        check(requests == 1 && s.phase == .dictated && !s.error.isEmpty &&
              f.proxy.view.string == dictated && f.proxy.view.selectedRange().length == 0,
              "mismatch retried the model, overwrote text or left selection")
        let keys = f.keys
        s.applyEdit(text: "迟到结果", revision: oldRevision, error: nil); f.drain(s)
        check(f.keys == keys && f.proxy.view.string == dictated, "late model result wrote text")
        f.proxy.unselectedCachedSuffix = ""
        s.cancel(); f.drain(s)
        check(s.phase == .undone && f.proxy.view.string == original, "mismatch lost instruction undo")
    }
}

test("Long model wait retains marked instruction with no selection until one replacement") {
    for caret in [2, 4] {
        let f = WeChatFixture("前文后文", caret: caret, codex: true, directRange: false)
        let s = try f.session()
        var requests = 0
        s.onEdit = { source, instruction, _ in
            requests += 1
            check(source == "前文后文" && instruction == "扩写", "model context changed")
        }
        s.update("扩写", final: false); s.convert(); s.update("扩写", final: true)
        let dictated = f.proxy.view.string
        let mark = f.proxy.view.markedRange()
        for _ in 0..<500 { f.now += 0.06; f.adapter.advanceClientOperation(); s.advanceReadback() }
        check(requests == 1 && s.phase == .editing && f.keys.isEmpty &&
              f.proxy.view.markedRange() == mark && f.proxy.view.string == dictated,
              "long model wait changed the field or repeated a request")
        s.applyEdit(text: "扩写后的正文", revision: s.revision, error: nil); f.drain(s)
        check(s.phase == .edited && requests == 1 && f.proxy.view.string == "扩写后的正文" &&
              !f.proxy.view.hasMarkedText() && f.keys.filter { $0 == "select_all" }.count == 1,
              "result did not apply in a single selection")
        s.cancel(); f.drain(s)
        check(s.phase == .dictated && f.proxy.view.string == dictated, "edit undo lost dictation")
        s.cancel(); f.drain(s)
        check(s.phase == .undone && f.proxy.view.string == "前文后文", "instruction undo lost source")
    }
}

test("Cancelling during model wait retains dictation without another full selection") {
    let f = WeChatFixture("前文后文", caret: 2, codex: true, directRange: false)
    let s = try f.session()
    s.update("修改", final: false); s.convert(); s.update("修改", final: true); f.drain(s)
    let keys = f.keys
    s.cancel(); f.drain(s)
    check(s.phase == .dictated && f.proxy.view.string == "前文修改后文" && f.keys == keys,
          "cancellation selected/replaced the full field or lost dictation")
    s.cancel(); f.drain(s)
    check(s.phase == .undone && f.proxy.view.string == "前文后文", "cancelled model instruction cannot be undone")
}

test("Cancelling after the model but before selected replacement never writes its candidate") {
    for ticks in [0, 1, 2] {
        let f = WeChatFixture("前文后文", caret: 2, codex: true, directRange: false)
        let s = try f.session()
        s.update("修改", final: false); s.convert(); s.update("修改", final: true)
        let revision = s.revision
        s.applyEdit(text: "不应写入的候选", revision: revision, error: nil)
        for _ in 0..<ticks { f.now += 0.06; f.adapter.advanceClientOperation(); s.advanceReadback() }
        s.cancel(); f.drain(s)
        check(s.phase == .dictated && f.proxy.view.string == "前文修改后文" &&
              f.proxy.view.selectedRange() == NSRange(location: 4, length: 0), "pre-write cancellation applied a candidate or left selection")
        check(f.proxy.insertCount == 1, "cancelled candidate was written and then restored")
        s.applyEdit(text: "迟到", revision: revision, error: nil); f.drain(s)
        s.cancel(); f.drain(s)
        check(s.phase == .undone && f.proxy.view.string == "前文后文", "cancelled instruction could not be undone")
    }
}

test("Actual source change is rejected at replacement without retrying the model") {
    let f = WeChatFixture("原文", codex: true, directRange: false)
    let s = try f.session()
    var requests = 0
    s.onEdit = { _, _, _ in requests += 1 }
    s.update("修改", final: false); s.convert(); s.update("修改", final: true)
    let revision = s.revision
    // Simulate an application-side source change with no input event.
    f.proxy.onInsert = {
        f.proxy.onInsert = nil
        f.proxy.view.textStorage?.replaceCharacters(in: NSRange(location: 0, length: 2), with: "变文")
    }
    s.applyEdit(text: "旧原文的结果", revision: revision, error: nil); f.drain(s)
    check(requests == 1 && s.phase == .dictated && !s.error.isEmpty && f.proxy.view.string == "变文修改",
          "source change retried a model or overwrote new text: requests=\(requests), phase=\(s.phase), error=\(s.error), text=\(f.proxy.view.string), keys=\(f.keys)")
    check(f.proxy.view.selectedRange() == NSRange(location: 4, length: 0), "failure left a full selection")
}

test("Model failure keeps the visible instruction and never selects the document") {
    let f = WeChatFixture("已有正文", codex: true, directRange: false)
    let s = try f.session()
    s.update("扩写", final: false); s.convert(); s.update("扩写", final: true)
    check(f.keys.isEmpty && f.proxy.view.hasMarkedText(), "waiting for model changed the input field")
    s.applyEdit(text: "", revision: s.revision, error: "模型服务暂时不可用"); f.drain(s)
    check(f.keys.isEmpty && s.phase == .dictated && f.proxy.view.string == "已有正文扩写",
          "model error selected the document or removed dictation")
}

test("Empty model result replaces the selected document once and undo restores dictation") {
    let f = WeChatFixture("全部删除", codex: true, directRange: false)
    let s = try f.session()
    s.update("清空", final: false); s.convert(); s.update("清空", final: true)
    s.applyEdit(text: "", revision: s.revision, error: nil); f.drain(s)
    check(s.phase == .edited && f.proxy.view.string.isEmpty && f.keys == ["select_to_start", "select_all", "delete"],
          "empty result failed or deleted twice")
    s.cancel(); f.drain(s)
    check(s.phase == .dictated && f.proxy.view.string == "全部删除清空", "empty edit could not restore dictation")
}

test("Confirmed context follows own dictation and undo without reusing a stale suffix") {
    let f = WeChatFixture("第一句。", codex: true, directRange: false)
    let first = try f.session()
    first.update("扩写", final: false); first.convert(); first.update("扩写", final: true)
    first.applyEdit(text: "第一句扩写。", revision: first.revision, error: nil); f.drain(first)
    f.proxy.unselectedCachedSuffix = "已删除的旧段落。"
    let spoken = try f.session()
    spoken.update("下一", final: false); spoken.update("下一句。", final: true)
    spoken.cancel(); f.drain(spoken)
    check(spoken.phase == .undone && f.proxy.view.string == "第一句扩写。", "hint tracking disturbed sentence undo")
    let next = try f.session()
    var requests = 0
    next.onEdit = { source, _, _ in
        requests += 1
        check(source == "第一句扩写。", "confirmed context reused phantom text after dictation undo")
    }
    let keys = f.keys
    next.update("精简", final: false); next.convert(); next.update("精简", final: true)
    check(f.keys == keys, "hint reading selected the document")
    next.applyEdit(text: "简文。", revision: next.revision, error: nil); f.drain(next)
    check(next.phase == .edited && requests == 1 && f.proxy.view.string == "简文。", "known document caused another model round")
    next.cancel(); f.drain(next); next.cancel(); f.drain(next)
    let afterUndo = try f.session()
    check(afterUndo.editOriginal?.text == "第一句扩写。", "edit undo left a stale context hint")
    f.adapter.discardContextHint()
    let uncached = try f.adapter.snapshot()
    check(uncached.readableContext?.text != "第一句扩写。", "manual takeover did not discard the context hint")
}

test("Committed dictation rebases cached composition coordinates and remains undoable") {
    for bundle in ["com.openai.codex", "com.microsoft.VSCode", "test.generic.editor"] {
        for prefix in ["", "原文😀\n"] {
            let suffix = "。后文保留"
            let proxy = StrictIMKClient(prefix + suffix, selection: NSRange(location: (prefix as NSString).length, length: 0))
            proxy.bundle = bundle
            proxy.compositionCoordinateOffset = 100
            let adapter = NativeInputClient(proxy)
            let session = try CompositionSession(client: adapter, utteranceID: "commit-rebase", sequence: 1)
            check(session.cancelLabel == "取消", "live dictation label is not cancel")
            session.update("今天", final: false)
            session.update("今天下午开会😀", final: false)
            session.finish()
            check(session.cancelLabel == "取消", "pending final label is not cancel")
            session.update("今天下午开会😀。", final: true)
            check(session.phase == .dictated && !session.awaitingReadback && session.canCancel,
                  "coordinate switch after commit removed undo")
            check(session.cancelLabel == "撤销", "committed label is not undo")
            check(session.readbackDiagnostics["ack_source"] as? String == "committed_range", "commit was not locally verified")
            check(!session.canReplace && session.expectedSelection == proxy.view.selectedRange(), "rebase gained full-document rights or kept the old caret")
            session.checkSelection()
            check(!session.awaitingReadback, "postcommit polling rejected the recovered coordinates")
            check(session.undoRecord()?.committedRange?.location == (prefix as NSString).length, "history retained the obsolete origin")
            session.cancel()
            check(session.phase == .undone && proxy.view.string == prefix + suffix, "undo damaged the surrounding text")
            check(proxy.insertCount == 2, "recovery replayed the native commit")
        }
    }
}

test("Commit settles without granting undo when local coordinates are unconfirmed") {
    for readFailure in ["unavailable", "wrong-text"] {
        let proxy = StrictIMKClient("")
        proxy.compositionCoordinateOffset = 100
        let adapter = NativeInputClient(proxy)
        var now: TimeInterval = 0
        let session = try CompositionSession(client: adapter, utteranceID: "commit-no-proof", sequence: 1, clock: { now })
        session.update("今天", final: false)
        proxy.onInsert = { if readFailure == "unavailable" { proxy.unavailableDocumentRead = true } else { proxy.staleDocumentRead = true } }
        session.update("今天", final: true)
        check(!session.awaitingReadback && !session.committedRangeVerified, "unreadable caret range blocked commit or enabled undo")
        now = 0.6; session.advanceReadback()
        check(session.phase == .dictated && !session.canCancel && proxy.insertCount == 1, "uncertain undo range failed dictation or replayed a write")
    }
}

test("Commit coordinate recovery does not follow a moved caret to an earlier identical sentence") {
    let proxy = StrictIMKClient("今天中间", selection: NSRange(location: 4, length: 0))
    let adapter = NativeInputClient(proxy)
    var now: TimeInterval = 0
    let session = try CompositionSession(client: adapter, utteranceID: "commit-moved", sequence: 1, clock: { now })
    session.update("今天", final: false)
    proxy.onInsert = { proxy.view.setSelectedRange(NSRange(location: 2, length: 0)) }
    session.update("今天", final: true)
    check(!session.awaitingReadback && !session.committedRangeVerified, "ambiguous caret was mistaken for an undo range")
    now = 0.6; session.advanceReadback()
    check(session.phase == .dictated && !session.canCancel && proxy.view.string == "今天中间今天", "uncertain caret failed dictation or changed an earlier sentence")
}

test("Live composition range follows coherent client coordinates across partials and polling") {
    let proxy = StrictIMKClient("前文")
    proxy.compositionCoordinateOffset = 100
    let adapter = NativeInputClient(proxy)
    let session = try CompositionSession(client: adapter, utteranceID: "live-coordinates", sequence: 1)
    session.update("一", final: false)
    for offset in [60, 0, 30] {
        proxy.compositionCoordinateOffset = offset
        session.checkSelection()
        check(!session.awaitingReadback && session.active, "coherent range change was mistaken for user movement")
        session.update("一二三", final: false)
        check(!session.awaitingReadback && session.phase == .listening, "partial retained an obsolete origin")
    }
    session.update("一二三。", final: true)
    check(session.phase == .dictated && session.committedRange == NSRange(location: 2, length: 4), "commit used a draft coordinate")
    session.cancel()
    check(session.phase == .undone && proxy.view.string == "前文", "undo used original snapshot coordinates")
}

test("Cancelling a live draft ends the owned composition after its coordinate origin changes") {
    let proxy = StrictIMKClient("前文")
    proxy.compositionCoordinateOffset = 100
    let session = try CompositionSession(client: NativeInputClient(proxy), utteranceID: "cancel-coordinates", sequence: 1)
    session.update("取消本句", final: false)
    proxy.compositionCoordinateOffset = 60
    session.cancel()
    check(session.phase == .undone && !session.awaitingReadback && proxy.view.string == "前文", "cancel required a stale draft caret")
}

test("Undo history retains committed spans without rewriting original context snapshots") {
    let proxy = StrictIMKClient("")
    proxy.compositionCoordinateOffset = 100
    let adapter = NativeInputClient(proxy)
    let history = CompositionUndoHistory()
    history.configure(enabled: true)
    let first = try CompositionSession(client: adapter, utteranceID: "history-first", sequence: 1)
    first.update("第一句。", final: true)
    check(first.original.selection.location == 100 && first.committedRange?.location == 0, "original context and committed range were conflated")
    history.settled(first)
    let second = try CompositionSession(client: adapter, utteranceID: "history-second", sequence: 1)
    history.begin(second.original)
    second.update("第二句。", final: true)
    check(history.count == 1 && second.phase == .dictated, "history rejected a confirmed committed range")
    second.cancel()
    check(proxy.view.string == "第一句。", "current undo damaged an older sentence")
    let record = history.pop()!
    let restored = CompositionSession(client: adapter, undo: record, utteranceID: "history-undo", sequence: 1, revision: 1)
    restored.cancel()
    check(restored.phase == .undone && proxy.view.string.isEmpty, "older undo used the draft origin")
}


test("Every native app ignores cached caret positions through streaming and commit") {
    for bundle in ["com.openai.codex", "com.tencent.xinWeChat", "com.microsoft.VSCode", "test.generic.editor"] {
        for reported in [NSRange(location: 0, length: 0), NSRange(location: 6, length: 0),
                         NSRange(location: 1000, length: 0), unspecifiedRange] {
            let proxy = StrictIMKClient("前文")
            proxy.bundle = bundle
            let session = try CompositionSession(client: NativeInputClient(proxy), utteranceID: "cached-caret", sequence: 1)
            proxy.fixedReportedSelection = reported
            for text in ["一", "一二三", "一二三四五六七八"] {
                session.update(text, final: false)
                session.checkSelection()
                check(session.phase == .listening && !session.awaitingReadback, "cached caret paused live ASR in \(bundle)")
            }
            session.update("一二三四五六七八。", final: true)
            check(session.phase == .dictated && !session.awaitingReadback && proxy.insertCount == 1,
                  "cached or unavailable caret blocked final in \(bundle): \(session.error)")
            check(proxy.view.string == "前文一二三四五六七八。" && !proxy.view.hasMarkedText(), "commit lost, duplicated, or retained marked text")
            session.checkSelection()
            check(session.phase == .dictated && !session.awaitingReadback, "postcommit polling stopped completed dictation")
            proxy.fixedReportedSelection = nil
            let next = try CompositionSession(client: NativeInputClient(proxy), utteranceID: "next-caret", sequence: 1)
            next.update("下一句", final: true)
            check(next.phase == .dictated && proxy.view.string.hasSuffix("。下一句"), "cached caret stranded next sentence")
        }
    }
}

test("Starting snapshot skips old marks and preserves selected text for undo") {
    let proxy = StrictIMKClient("甲乙丙", selection: NSRange(location: 1, length: 1))
    proxy.reportedMarkedRanges = [NSRange(location: 0, length: 900)]
    let adapter = NativeInputClient(proxy)
    let session = try CompositionSession(client: adapter, utteranceID: "selected-start", sequence: 1)
    check(proxy.markedReads == 0 && proxy.reportedMarkedRanges.count == 1, "begin consulted stale marked state")
    check(session.original.selectedText == "乙" && session.original.selection == NSRange(location: 1, length: 1), "selected original was lost")
    proxy.reportedMarkedRanges = []
    session.update("新字", final: false)
    check(proxy.view.string == "甲新字丙", "native input failed to replace selection")
    session.cancel()
    check(proxy.view.string == "甲乙丙", "cancel failed to restore selected original")
}

print("\(passed) production input adapter tests passed")
