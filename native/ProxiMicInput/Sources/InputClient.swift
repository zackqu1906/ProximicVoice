import AppKit
import InputMethodKit
import Carbon

final class NativeInputClient: HandoffClient {
    let client: IMKTextInput
    private(set) var activeRead = ""
    private(set) var lastReadDiagnostics: [String: Any] = [:]
    // Model context only, never write authorization. Keep our own confirmed
    // document and subsequent IME changes out of a client's stale suffix cache.
    // Native selection still verifies every candidate before replacement.
    private var modelContextHint: String?
    private var hintedMarkedRange: NSRange?
    private var hintAfterUndo: String?
    private var hintGeneration = 0
    private let isCurrent: () -> Bool
    private let preeditAttributes: ((NSRange) -> [NSAttributedString.Key: Any]?)?
    init(_ client: IMKTextInput, isCurrent: @escaping () -> Bool = { true }, preeditAttributes: ((NSRange) -> [NSAttributedString.Key: Any]?)? = nil) {
        self.client = client
        self.isCurrent = isCurrent
        self.preeditAttributes = preeditAttributes
    }
    var compatibility: WeChatCompatibility?
    // Default edits select the native document before one IMK commit. Explicit
    // replacement ranges alone can leave old text behind in custom editors.
    var nativeCompatibility: WeChatCompatibility?
    var pendingCompatibility: WeChatCompatibility? { compatibility ?? nativeCompatibility }
    func configureKeyDelivery(_ sendKey: @escaping WeChatCompatibility.SendKey,
                              clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        let helper = WeChatCompatibility(client: self, sendKey: sendKey, clock: clock,
                                        selectBeforeReplacing: client.bundleIdentifier() != "com.tencent.xinWeChat")
        if client.bundleIdentifier() == "com.tencent.xinWeChat" { compatibility = helper }
        else { nativeCompatibility = helper }
    }
    // IMK document substring caches can lag live marked text, even while the
    // marked range is current (observed in both Codex and VS Code). Use live
    // composition geometry for every client's partial ACK; committed document
    // edits and undo still require exact text readback.
    var compositionTextReadbackReliable: Bool { false }
    var clientOperationPending: Bool { pendingCompatibility?.pending ?? false }
    var clientOperationFailure: String? { pendingCompatibility?.failure }
    var clientOperationDiagnostics: [String: Any] { pendingCompatibility?.readbackDiagnostics ?? [:] }
    var verifiedReplacementCaret: NSRange? { nativeCompatibility?.verifiedReplacementCaret }
    var verifiedReplacement: EditorTextRange? { pendingCompatibility?.verifiedReplacement }
    var preparesEditContext: Bool { nativeCompatibility?.selectBeforeReplacing ?? false }
    var preparedEditContext: PreparedEditContext? { nativeCompatibility?.preparedEditContext }
    func prepareEditContext(instruction: String, selectedText: String, source: EditorTextRange?, candidate: String) {
        nativeCompatibility?.prepareContext(instruction: instruction, selectedText: selectedText,
                                            source: source, candidate: candidate)
    }
    func discardEditCandidate() { nativeCompatibility?.discardEditCandidate() }
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?, expected: String) {
        if let nativeCompatibility, nativeCompatibility.selectBeforeReplacing {
            nativeCompatibility.replaceSelected(range, before: expected, with: text, restoringCaret: restoringCaret)
        } else { replaceEditing(range, with: text, restoringCaret: restoringCaret) }
    }
    func cancelClientOperation() {
        if clientOperationPending { discardContextHint() }
        pendingCompatibility?.cancel()
    }
    func discardContextHint() {
        hintGeneration += 1
        modelContextHint = nil; hintedMarkedRange = nil; hintAfterUndo = nil
    }
    func advanceClientOperation() {
        let wasPending = clientOperationPending
        pendingCompatibility?.advance()
        if wasPending, !clientOperationPending, nativeCompatibility != nil {
            if clientOperationFailure != nil { discardContextHint() }
            else if let document = nativeCompatibility?.verifiedDocument {
                modelContextHint = document; hintedMarkedRange = nil; hintAfterUndo = nil
            } else if let document = hintAfterUndo {
                modelContextHint = document; hintedMarkedRange = nil; hintAfterUndo = nil
            }
        }
    }
    func undo(_ range: NSRange, with text: String, preferNative: Bool) {
        hintAfterUndo = updatedHint(in: range, with: text)
        modelContextHint = nil; hintedMarkedRange = nil
        if let helper = pendingCompatibility { helper.undoSentence(range, with: text) }
        else { replace(range, with: text) }
    }

    private func updatedHint(in range: NSRange, with text: String) -> String? {
        guard let document = modelContextHint, isValidRange(range, length: (document as NSString).length) else { return nil }
        let result = (document as NSString).replacingCharacters(in: range, with: text)
        return (result as NSString).length <= 512 * 1024 ? result : nil
    }

    private func rangeNumbers(_ range: NSRange) -> [Int] {
        isValidRange(range) ? [range.location, range.length] : [-1, -1]
    }

    private func documentSelection(_ selected: NSRange, marked: NSRange) -> NSRange {
        // Clients can report the whole composition selected with a cached
        // origin (zero or an earlier composition's start). markedRange is current.
        // Only normalize a positive full-composition selection, never a caret.
        if !compositionTextReadbackReliable, isValidRange(marked), marked.length > 0,
           isValidRange(selected), selected.length == marked.length { return marked }
        return selected
    }

    func snapshot() throws -> EditorSnapshot { try readSnapshot(includeComposition: true) }
    func snapshotForBeginning() throws -> EditorSnapshot { try readSnapshot(includeComposition: false) }
    func startingSelection() -> NSRange { client.selectedRange() }

    private func validateReadLease() throws {
        guard isCurrent() else { throw CompositionError.interrupted("输入连接已更新，已丢弃旧读取") }
    }

    private func readSnapshot(includeComposition: Bool) throws -> EditorSnapshot {
        try validateReadLease()
        let previousRead = activeRead
        defer { activeRead = previousRead }
        activeRead = "snapshot.selectedRange"
        let reportedSelection = client.selectedRange()
        try validateReadLease()
        activeRead = includeComposition ? "snapshot.markedRange" : "snapshot.beginning"
        let marked = includeComposition ? client.markedRange() : unspecifiedRange
        try validateReadLease()
        let selected = documentSelection(reportedSelection, marked: marked)
        activeRead = "snapshot.supportsProperty"
        let access = client.supportsProperty(TSMDocumentPropertyTag(kTSMDocumentSupportDocumentAccessPropertyTag))
        try validateReadLease()
        var whole: String?
        var readable: EditorTextRange?
        activeRead = "snapshot.length"
        let length = client.length()
        try validateReadLease()
        lastReadDiagnostics = ["query": "snapshot", "document_length": length,
                               "reported_selection": rangeNumbers(reportedSelection),
                               "selection": rangeNumbers(selected), "marked_range": rangeNumbers(marked),
                               "document_access": access, "composition_queried": includeComposition, "text_queried": false, "returned_characters": -1,
                               "actual_range": [-1, -1]]
        // Do not misclassify an unavailable/oversized document as an empty one.
        if length == 0, isValidRange(marked), marked.length > 0 {
            // An independently readable composition contradicts an empty full
            // document. Keep local composition usable without claiming context.
            whole = nil
        } else if length > 0, length != NSNotFound, length <= 512 * 1024 {
            var actual = unspecifiedRange
            let desired = NSRange(location: 0, length: length)
            lastReadDiagnostics["text_queried"] = true
            activeRead = "snapshot.documentString"
            let value = client.string(from: desired, actualRange: &actual)
            try validateReadLease()
            lastReadDiagnostics["returned_characters"] = value.map { ($0 as NSString).length } ?? -1
            lastReadDiagnostics["actual_range"] = rangeNumbers(actual)
            if let value, actual == desired, (value as NSString).length == length {
                whole = value
            }
        }
        if whole == nil, let hint = modelContextHint, isValidRange(selected, length: (hint as NSString).length) {
            readable = EditorTextRange(text: hint, range: NSRange(location: 0, length: (hint as NSString).length))
            lastReadDiagnostics["context_queries"] = 0
            lastReadDiagnostics["context_characters"] = (hint as NSString).length
        } else if whole == nil {
            var queries = 0
            readable = EditorContextReader.read(around: selected) { requested in
                guard isCurrent() else { return nil }
                queries += 1
                var actual = unspecifiedRange
                activeRead = "snapshot.contextString"
                let value = client.string(from: requested, actualRange: &actual)
                guard isCurrent() else { return nil }
                lastReadDiagnostics["context_requested_range"] = rangeNumbers(requested)
                lastReadDiagnostics["context_actual_range"] = rangeNumbers(actual)
                lastReadDiagnostics["context_returned_characters"] = value.map { ($0 as NSString).length } ?? -1
                return value.map { EditorTextRange(text: $0, range: actual) }
            }
            try validateReadLease()
            lastReadDiagnostics["context_queries"] = queries
            lastReadDiagnostics["context_readable_range"] = readable.map { rangeNumbers($0.range) } ?? [-1, -1]
            lastReadDiagnostics["context_characters"] = readable?.range.length ?? -1
        }
        // A positive marked range can outlive the reported document length.
        // Query that exact range rather than treating an unqueried -1 as nil.
        // Diagnostics deliberately contain only lengths/ranges, never text.
        lastReadDiagnostics["marked_text_queried"] = false
        if isValidRange(marked), marked.length > 0, marked.length <= 512 * 1024 {
            var actual = unspecifiedRange
            activeRead = "snapshot.markedString"
            let value = client.string(from: marked, actualRange: &actual)
            try validateReadLease()
            lastReadDiagnostics["marked_text_queried"] = true
            lastReadDiagnostics["marked_returned_characters"] = value.map { ($0 as NSString).length } ?? -1
            lastReadDiagnostics["marked_actual_range"] = rangeNumbers(actual)
        }
        var selectedText: String?
        if let whole, isValidRange(selected, length: (whole as NSString).length) {
            selectedText = (whole as NSString).substring(with: selected)
        } else if isValidRange(selected), selected.length == 0 {
            selectedText = ""
        } else if isValidRange(selected) {
            var actual = unspecifiedRange
            activeRead = "snapshot.selectedString"
            if let value = client.string(from: selected, actualRange: &actual), actual == selected,
               (value as NSString).length == selected.length { selectedText = value }
        }
        try validateReadLease()
        return EditorSnapshot(text: whole, selection: selected, markedRange: marked,
                              selectedText: selectedText, documentAccess: access, readableContext: readable)
    }

    func probe() -> CompositionProbe {
        let reportedSelection = client.selectedRange()
        let marked = client.markedRange()
        let selected = documentSelection(reportedSelection, marked: marked)
        lastReadDiagnostics = ["query": "probe", "selection": rangeNumbers(selected),
                               "reported_selection": rangeNumbers(reportedSelection),
                               "marked_range": rangeNumbers(marked), "text_queried": false, "returned_characters": -1,
                               "actual_range": [-1, -1]]
        var markedText: String?
        lastReadDiagnostics["composition_geometry_only"] = !compositionTextReadbackReliable
        if compositionTextReadbackReliable, isValidRange(marked), marked.length <= 512 * 1024 {
            var actual = unspecifiedRange
            lastReadDiagnostics["text_queried"] = true
            let text = client.string(from: marked, actualRange: &actual)
            lastReadDiagnostics["returned_characters"] = text.map { ($0 as NSString).length } ?? -1
            lastReadDiagnostics["actual_range"] = rangeNumbers(actual)
            if let text, actual == marked, (text as NSString).length == marked.length {
                markedText = text
            }
        }
        return CompositionProbe(selection: selected, markedRange: marked, markedText: markedText, reportedSelection: reportedSelection)
    }

    func selection() -> NSRange { documentSelection(client.selectedRange(), marked: client.markedRange()) }
    func readText(in range: NSRange) -> String? {
        guard isValidRange(range), range.length <= 512 * 1024 else { return nil }
        // An empty replacement is checked by its caret and cleared marked
        // range; IMK may legitimately return nil for a zero-length substring.
        if range.length == 0 { return "" }
        var actual = unspecifiedRange
        let text = client.string(from: range, actualRange: &actual)
        lastReadDiagnostics = ["query": "range", "text_queried": true,
                               "requested_range": rangeNumbers(range), "actual_range": rangeNumbers(actual),
                               "returned_characters": text.map { ($0 as NSString).length } ?? -1]
        guard let text, actual == range, (text as NSString).length == range.length else { return nil }
        return text
    }
    func mark(_ text: String) {
        if modelContextHint != nil {
            let range = hintedMarkedRange ?? selection()
            modelContextHint = updatedHint(in: range, with: text)
            hintedMarkedRange = modelContextHint == nil ? nil : NSRange(location: range.location, length: (text as NSString).length)
        }
        let range = NSRange(location: 0, length: (text as NSString).length)
        // Use the IMK controller's system marking, including its clause and
        // underline color/style. A hand-written clause 0 + generic underline
        // is not equivalent to the system's selected raw-text marking.
        let attributes = preeditAttributes?(range) ?? [
            .underlineStyle: NSUnderlineStyle.double.rawValue,
            .underlineColor: NSColor.textColor
        ]
        let marked = NSAttributedString(string: text, attributes: attributes)
        client.setMarkedText(marked, selectionRange: NSRange(location: marked.length, length: 0), replacementRange: unspecifiedRange)
    }
    func commit(_ text: String) {
        let generation = hintGeneration
        let hint = modelContextHint == nil ? nil : updatedHint(in: hintedMarkedRange ?? selection(), with: text)
        CompositionCommitDelivery.send(text, markedRange: { client.markedRange() },
                                       clearMarkedText: { mark("") },
                                       insertText: { client.insertText($0, replacementRange: unspecifiedRange) })
        if generation == hintGeneration { modelContextHint = hint; hintedMarkedRange = nil }
    }
    func replace(_ range: NSRange, with text: String) {
        if let compatibility { compatibility.replace(range, with: text, undo: false); return }
        // Committed document writes use insertText in every default client.
        // Empty marked text cancels preedit; it is not a portable deletion API.
        guard isValidRange(range) else { return }
        if let nativeCompatibility, !text.isEmpty || nativeCompatibility.selectBeforeReplacing {
            nativeCompatibility.replaceNative(range, with: text) { self.client.insertText(text, replacementRange: range) }
            return
        }
        CommittedReplacementDelivery.send(text, range: range,
            insert: { client.insertText($0, replacementRange: $1) })
    }
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?) {
        if let nativeCompatibility, !text.isEmpty || nativeCompatibility.selectBeforeReplacing {
            nativeCompatibility.replaceNative(range, with: text, restoringCaret: restoringCaret) {
                self.client.insertText(text, replacementRange: range)
            }
        } else { replace(range, with: text) }
    }
    func endStaleComposition(at caret: NSRange) {
        discardContextHint()
        guard isValidRange(caret), caret.length == 0 else { return }
        // Empty insertText can be ignored across the IMK client bridge. This
        // activation-only operation updates an unreadable, detached cache at
        // the verified caret without replacing a committed document range.
        client.setMarkedText("", selectionRange: NSRange(location: 0, length: 0), replacementRange: caret)
    }

    private(set) var caretDiagnostics: [String: Any] = [:]
    func caret(compositionLength: Int, trace: Bool = false) -> NSRect? {
        var diagnostic: [String: Any] = [:]
        if trace { diagnostic["composition_length"] = compositionLength }
        defer {
            if trace {
                var candidate = NSRect.zero
                _ = client.attributes(forCharacterIndex: compositionLength, lineHeightRectangle: &candidate)
                diagnostic["candidate_end"] = CaretDiagnostics.rect(candidate)
                var last = NSRect.zero
                _ = client.attributes(forCharacterIndex: max(0, compositionLength - 1), lineHeightRectangle: &last)
                diagnostic["candidate_last"] = CaretDiagnostics.rect(last)
                var actual = unspecifiedRange
                let relative = client.firstRect(forCharacterRange: NSRange(location: compositionLength, length: 0), actualRange: &actual)
                diagnostic["relative_rect"] = CaretDiagnostics.rect(relative)
                diagnostic["relative_actual"] = CaretDiagnostics.range(actual)
                caretDiagnostics = diagnostic
            }
        }
        // A valid IMK candidate anchor may stay at the start of the inline
        // session. Query the live insertion point before that fallback so the
        // palette follows growth, wrapping and scrolling of marked text.
        // markedRange uses document coordinates; selectedRange can instead
        // report a cached, composition-relative selection in bridged clients.
        var insertion = unspecifiedRange
        if compositionLength > 0 {
            let marked = client.markedRange()
            if trace { diagnostic["marked"] = CaretDiagnostics.range(marked) }
            if isValidRange(marked), marked.length > 0 {
                insertion = NSRange(location: NSMaxRange(marked), length: 0)
            }
        }
        if !isValidRange(insertion) {
            let selected = client.selectedRange()
            if trace { diagnostic["selected"] = CaretDiagnostics.range(selected) }
            if isValidRange(selected) {
                insertion = NSRange(location: NSMaxRange(selected), length: 0)
            }
        }
        if isValidRange(insertion) {
            var actual = unspecifiedRange
            let rectangle = client.firstRect(forCharacterRange: insertion, actualRange: &actual)
            if trace {
                diagnostic["query"] = CaretDiagnostics.range(insertion)
                diagnostic["first_rect"] = CaretDiagnostics.rect(rectangle)
                diagnostic["actual"] = CaretDiagnostics.range(actual)
                if insertion.location > 0 {
                    var glyphActual = unspecifiedRange
                    let glyph = client.firstRect(forCharacterRange: NSRange(location: insertion.location - 1, length: 1), actualRange: &glyphActual)
                    diagnostic["last_glyph_rect"] = CaretDiagnostics.rect(glyph)
                    diagnostic["last_glyph_actual"] = CaretDiagnostics.range(glyphActual)
                }
            }
            // A substituted whole-selection/first-line rectangle is not the
            // requested caret, even when it lies on screen.
            if actual == insertion, let rectangle = visibleCaret(rectangle) {
                if trace { diagnostic["source"] = "document_caret" }
                return rectangle
            }
        }
        var rectangle = NSRect.zero
        // The inline API takes a CHARACTER index, not an insertion offset.
        // length is one past the last character and real IMK bridges return
        // NSZeroRect there. Index the final UTF-16 character to follow the
        // current line; with no composition, 0 means the current selection.
        let inlineIndex = max(0, compositionLength - 1)
        _ = client.attributes(forCharacterIndex: inlineIndex, lineHeightRectangle: &rectangle)
        if trace {
            diagnostic["inline_index"] = inlineIndex
            diagnostic["source"] = visibleCaret(rectangle) == nil ? "unavailable" : "inline_character"
        }
        return visibleCaret(rectangle)
    }

    private func visibleCaret(_ rectangle: NSRect) -> NSRect? {
        guard rectangle.width.isFinite, rectangle.height.isFinite,
              rectangle.origin.x.isFinite, rectangle.origin.y.isFinite,
              rectangle.height > 0, rectangle.height < 1000,
              NSScreen.screens.contains(where: { $0.frame.intersects(rectangle.insetBy(dx: -1, dy: -1)) }) else { return nil }
        return rectangle.standardized
    }
}
