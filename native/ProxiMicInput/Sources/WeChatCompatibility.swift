import Foundation

// IMK substring windows can include terminal paragraph separators outside the
// editor's native document selection. Compare representations at the selection
// boundary, without trimming either the real document or the model result.
enum EditSourceComparison: String {
    case exact, terminalParagraphs, different

    static func compare(_ cached: EditorTextRange?, with selected: String) -> Self {
        guard let cached, cached.range.location == 0,
              cached.range.length == (cached.text as NSString).length else { return .different }
        if cached.text == selected { return .exact }
        let end = (selected as NSString).length
        guard cached.text.hasPrefix(selected), cached.range.length > end else { return .different }
        let surplus = (cached.text as NSString).substring(from: end)
        return surplus.unicodeScalars.allSatisfy { $0 == "\n" || $0 == "\r" || $0 == "\u{2029}" }
            ? .terminalParagraphs : .different
    }
}

/// Default edits use native selection then one IMK commit. Sentence undo selects
/// only its own span. WeChat retains its separate selection/readback handling.
/// Qt 5 ignores document replacement ranges;
/// select through its native commands, then write through the IME selection.
/// The host posts only tagged, process-targeted keys, never clipboard content.
final class WeChatCompatibility {
    typealias SendKey = (String, Int, Int64, @escaping (String?) -> Void) -> Void
    unowned let client: CompositionClient
    let sendKey: SendKey
    let clock: () -> TimeInterval
    let selectBeforeReplacing: Bool
    private(set) var pending = false
    private(set) var failure: String?
    private(set) var readbackDiagnostics: [String: Any] = [:]
    private(set) var eventTag: Int64 = 0
    private var stage = ""
    private var deadline: TimeInterval = 0
    private var earliest: TimeInterval = 0
    private var keyAcknowledged = false
    private var generation = 0
    private var expectedKeys: [(code: Int, command: Bool, shift: Bool)] = []
    private var keyDeliveryDeadline: TimeInterval = 0
    private var expectedRange = unspecifiedRange
    private var expectedBefore = ""
    private var replacement = ""
    private var result = ""
    private var resultCaret = 0
    private var requestedCaret: Int?
    private var caretMotionOrigin: Int?
    private var caretMotionCount = 0
    private(set) var verifiedReplacementCaret: NSRange?
    private(set) var verifiedReplacement: EditorTextRange?
    private var replacementTextVerified = false
    private var selectionWriteSent = false
    private var requestedReplacement: EditorTextRange?
    private var operationStartedAt: TimeInterval = 0
    private var selectionWrittenAt: TimeInterval = 0
    private(set) var preparedEditContext: PreparedEditContext?
    private(set) var verifiedDocument: String?
    private var contextInstruction = ""
    private var contextSelectedText = ""
    private var contextPrefix = ""
    private var contextWhole = ""
    private var contextOriginal: EditorSnapshot?
    private var contextSource: EditorTextRange?
    private var contextCandidate: String?

    init(client: CompositionClient, sendKey: @escaping SendKey,
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         selectBeforeReplacing: Bool = false) {
        self.client = client; self.sendKey = sendKey; self.clock = clock
        self.selectBeforeReplacing = selectBeforeReplacing
    }

    func cancel() {
        generation += 1; pending = false; stage = ""; eventTag = 0; expectedKeys = []
        verifiedReplacementCaret = nil; verifiedReplacement = nil; requestedReplacement = nil
        replacementTextVerified = false; selectionWriteSent = false
        preparedEditContext = nil; contextOriginal = nil; verifiedDocument = nil
    }

    /// Some IMK/Qt events lose CGEvent user data during conversion to NSEvent.
    /// In that case admit only a still-pending exact key/modifier sequence for
    /// 500ms. Never suppress arbitrary typing, repeat keys, or another client.
    func acceptsKey(code: Int, command: Bool, shift: Bool = false, otherModifiers: Bool, tag: Int64) -> Bool {
        guard pending, !otherModifiers, clock() < keyDeliveryDeadline,
              tag == 0 || tag == eventTag,
              let index = expectedKeys.firstIndex(where: { $0.code == code && $0.command == command && $0.shift == shift }) else { return false }
        expectedKeys.removeFirst(index + 1) // AppKit may consume Cmd+A before IMK sees it.
        return true
    }

    func replace(_ range: NSRange, with text: String, undo: Bool) {
        if undo { undoSentence(range, with: text); return }
        guard let before = client.readText(in: range) else { failure = "输入框未提供待替换的原文"; return }
        start(range: range, before: before, text: text)
    }

    func replaceSelected(_ range: NSRange, before: String, with text: String, restoringCaret: Int?) {
        start(range: range, before: before, text: text)
        requestedCaret = restoringCaret
    }

    func prepareContext(instruction: String, selectedText: String, source: EditorTextRange?, candidate: String) {
        cancel(); failure = nil; readbackDiagnostics = [:]
        contextInstruction = instruction; contextSelectedText = selectedText
        contextSource = source; contextCandidate = candidate
        contextPrefix = ""; contextWhole = ""
        operationStartedAt = clock()
        pending = true; stage = "context_committed"
        deadline = clock() + 2.5; earliest = clock(); keyAcknowledged = true
    }

    func discardEditCandidate() { contextCandidate = nil }

    func replaceNative(_ range: NSRange, with text: String, restoringCaret: Int? = nil, write: () -> Void) {
        if selectBeforeReplacing {
            // Custom editors can apply an explicit IMK range as a partial replacement
            // while its cache appears to contain the full result. Its native
            // selection gives the editor an unambiguous replacement target.
            guard let before = client.readText(in: range) else { failure = "输入框未提供待替换的原文"; return }
            start(range: range, before: before, text: text)
            requestedCaret = restoringCaret
            return
        }
        cancel(); failure = nil
        readbackDiagnostics = [:]
        let token = generation
        let probe = client.probe()
        guard generation == token else { return }
        guard isValidRange(range), !text.isEmpty,
              isValidRange(probe.selection), probe.selection.length == 0,
              !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else {
            failure = "输入框原文光标或输入组合已变化，未执行编辑"; return
        }
        result = text
        expectedRange = NSRange(location: range.location, length: (text as NSString).length)
        requestedReplacement = EditorTextRange(text: text, range: expectedRange)
        resultCaret = NSMaxRange(expectedRange)
        requestedCaret = restoringCaret
        caretMotionOrigin = nil; caretMotionCount = 0
        stage = "native_written"; pending = true
        deadline = clock() + 2.5; earliest = clock(); keyAcknowledged = true
        write() // Exactly one document write. No marked-text replacement.
    }

    private func confirmNativeCaret(_ probe: CompositionProbe) {
        let token = generation
        guard isValidRange(probe.selection), probe.selection.length == 0 else { return }
        // Cmd+Down establishes the document end independently of the old caret.
        // Cached text/selection may arrive in separate callbacks; wait only for
        // the resulting position, never require it to match a pre-write offset.
        // Rich editors can expose a trailing paragraph sentinel beyond their
        // visual caret. Do not try to prove EOF by reading past Cmd+Down's
        // result; the acknowledged native command establishes that position.
        guard pending, generation == token else { return }
        guard let requestedCaret else {
            guard probe.selection.location >= NSMaxRange(expectedRange) else { return }
            verifiedReplacementCaret = probe.selection
            verifiedReplacement = requestedReplacement
            pending = false; stage = ""; return
        }
        if probe.selection == NSRange(location: requestedCaret, length: 0) {
            verifiedReplacementCaret = probe.selection
            verifiedReplacement = requestedReplacement
            pending = false; stage = ""; return
        }
        // Cancelling an edit restores a dictated sentence that may be in the
        // middle. Put the caret after that sentence so a second undo deletes it.
        // Chromium can return the pre-replacement caret even after Cmd+Down.
        // Read back each move and correct in either direction instead of
        // treating the first calculated displacement as final. An unchanged
        // cache is not permission to repeat the same move.
        guard probe.selection.location != caretMotionOrigin else { return }
        let motionRange = NSRange(location: min(requestedCaret, probe.selection.location),
                                  length: abs(probe.selection.location - requestedCaret))
        guard requestedCaret >= expectedRange.location, requestedCaret <= NSMaxRange(expectedRange),
              let between = client.readText(in: motionRange),
              pending, generation == token else { return }
        guard between.count > 0, between.count <= 512 else { fail("恢复听写后的光标距离超出移动范围"); return }
        guard caretMotionCount < 4 else { fail("恢复听写的光标仍未稳定，已停止定位"); return }
        caretMotionOrigin = probe.selection.location
        caretMotionCount += 1
        key(probe.selection.location > requestedCaret ? "caret_backward" : "caret_forward",
            count: between.count, next: "native_restore_caret")
    }

    /// Select only the still-owned sentence, then delete once (or restore the
    /// selection it replaced). Never selects/copies the whole field or depends
    /// on Qt's undo grouping. Live preedit cancellation uses IME commit directly.
    func undoSentence(_ range: NSRange, with text: String) {
        cancel(); failure = nil
        readbackDiagnostics = [:]
        let probe = client.probe()
        guard isValidRange(range), range.length > 0,
              probe.selection == NSRange(location: NSMaxRange(range), length: 0),
              !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
              let before = client.readText(in: range),
              (before as NSString).length == range.length, before.count <= 512 else {
            failure = "本句文字或光标已变化，未执行撤销"; return
        }
        expectedRange = range; expectedBefore = before; replacement = text
        requestedReplacement = EditorTextRange(text: text, range: NSRange(location: range.location, length: (text as NSString).length))
        requestedCaret = nil
        resultCaret = range.location + (text as NSString).length
        pending = true; deadline = clock() + 2.5
        key("select_previous", count: before.count, next: "sentence_selected")
    }

    private func start(range: NSRange, before: String, text: String) {
        cancel(); failure = nil
        readbackDiagnostics = [:]
        operationStartedAt = clock()
        guard isValidRange(range), range.length == (before as NSString).length else {
            failure = "输入框原文范围无效"; return
        }
        expectedRange = range; expectedBefore = before; replacement = text
        requestedReplacement = EditorTextRange(text: text, range: NSRange(location: range.location, length: (text as NSString).length))
        requestedCaret = nil
        pending = true
        deadline = clock() + 2.5
        key("select_all", next: "capture")
    }

    private func key(_ command: String, count: Int = 0, next: String) {
        stage = next; keyAcknowledged = false
        eventTag = Int64.random(in: 1...(1 << 50))
        keyDeliveryDeadline = clock() + 0.5
        switch command {
        case "select_all": expectedKeys = [(0, true, false)]
        case "select_to_start": expectedKeys = [(126, true, true)]
        case "select_previous": expectedKeys = Array(repeating: (123, false, true), count: count)
        case "delete": expectedKeys = [(51, false, false)]
        case "caret_from_end": expectedKeys = [(125, true, false)] + Array(repeating: (123, false, false), count: count)
        case "caret_to_end": expectedKeys = [(125, true, false)]
        case "caret_backward": expectedKeys = Array(repeating: (123, false, false), count: count)
        case "caret_forward": expectedKeys = Array(repeating: (124, false, false), count: count)
        default: expectedKeys = []
        }
        let token = generation
        sendKey(command, count, eventTag) { [weak self] error in
            guard let self, self.pending, self.generation == token else { return }
            if let error { self.fail(error); return }
            self.keyAcknowledged = true
            // Posting isn't delivery. Also require selection/text readback.
            self.earliest = self.clock() + 0.04
        }
    }

    private func fail(_ message: String) { failure = message; cancel() }

    private func selectedDocument() -> String? {
        let probe = client.probe()
        guard !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
              isValidRange(probe.selection), probe.selection.location == 0,
              probe.selection.length <= 512 * 1024,
              let value = client.readText(in: probe.selection),
              (value as NSString).length == probe.selection.length else { return nil }
        // A dropped Cmd+A must not turn a prefix selection or an empty caret
        // into a claimed full document. Check the boundary using both lengths
        // so a surrogate pair at the boundary is not mistaken for no suffix.
        if selectBeforeReplacing { return value }
        let end = NSMaxRange(probe.selection)
        guard client.readText(in: NSRange(location: end, length: 1)) == nil,
              client.readText(in: NSRange(location: end, length: 2)) == nil else { return nil }
        return value
    }

    private func moveCaret() {
        let string = result as NSString
        guard resultCaret <= string.length else { fail("输入框恢复光标的位置无效"); return }
        let tail = string.substring(from: resultCaret)
        // Key motion uses composed characters; refuse a split grapheme.
        let prefix = string.substring(to: resultCaret)
        guard (prefix + tail).count == prefix.count + tail.count, tail.count <= 512 else {
            fail("输入框原文过长或光标位于复合字符内部，未移动光标"); return
        }
        if client.selection() == NSRange(location: resultCaret, length: 0) {
            stage = "verify"; keyAcknowledged = true; earliest = clock()
            return
        }
        key("caret_from_end", count: tail.count, next: "verify")
    }

    private func writeSelected(_ observed: String) {
        // capture already read the selected original in this same run-loop
        // turn. No second full-document query before the single native write.
        selectionWriteSent = true
        selectionWrittenAt = clock()
        if result.isEmpty {
            key("delete", next: "written")
        } else {
            client.commit(result)
            stage = "written"; keyAcknowledged = true; earliest = clock() + 0.04
        }
    }

    func advance() {
        guard pending else { return }
        guard clock() < deadline else {
            fail(stage.hasPrefix("native_") ? "输入框未确认编辑结果或末尾光标，已停止本次操作" : (stage.hasPrefix("sentence_") ? "输入框未确认本句选取或撤销结果，已停止本次操作" : "输入框未确认选区或替换结果，已停止本次操作"))
            return
        }
        guard keyAcknowledged, clock() >= earliest else { return }
        let token = generation
        switch stage {
        case "context_committed":
            let probe = client.probe()
            guard pending, generation == token, isValidRange(probe.selection), probe.selection.length == 0,
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else { return }
            key("select_to_start", next: "context_prefix")
        case "context_prefix":
            let probe = client.probe()
            guard isValidRange(probe.selection), probe.selection.location == 0,
                  let prefix = client.readText(in: probe.selection),
                  pending, generation == token else { return }
            guard prefix.hasSuffix(contextInstruction) else {
                fail("当前选区未包含刚才的口述指令，未调用模型"); return
            }
            contextPrefix = prefix
            key("select_all", next: "context_whole")
        case "context_whole":
            guard let whole = selectedDocument(), pending, generation == token else { return }
            guard whole.hasPrefix(contextPrefix) else {
                fail("当前正文在读取期间发生变化，未调用模型"); return
            }
            contextWhole = whole
            let instructionRange = NSRange(location: (contextPrefix as NSString).length - (contextInstruction as NSString).length,
                                           length: (contextInstruction as NSString).length)
            let source = (whole as NSString).replacingCharacters(in: instructionRange, with: contextSelectedText)
            contextOriginal = EditorSnapshot(text: nil,
                selection: NSRange(location: instructionRange.location, length: (contextSelectedText as NSString).length),
                markedRange: unspecifiedRange, selectedText: contextSelectedText, documentAccess: true,
                readableContext: EditorTextRange(text: source, range: NSRange(location: 0, length: (source as NSString).length)))
            let comparison = EditSourceComparison.compare(contextSource, with: source)
            readbackDiagnostics = ["expected_characters": contextSource?.range.length ?? -1,
                "observed_characters": (source as NSString).length,
                "text_matches": comparison != .different, "source_match": comparison.rawValue]
            if comparison != .different, let original = contextOriginal, let candidate = contextCandidate {
                // Source and native selection agree. Replace immediately while
                // selected; no restore/reselect cycle, clipboard, or second read.
                preparedEditContext = PreparedEditContext(original: original, dictatedText: whole,
                    caret: NSRange(location: (contextPrefix as NSString).length, length: 0))
                result = candidate
                resultCaret = (result as NSString).length
                requestedReplacement = EditorTextRange(text: result, range: NSRange(location: 0, length: resultCaret))
                writeSelected(whole)
                return
            }
            let tail = (whole as NSString).substring(from: (contextPrefix as NSString).length)
            guard tail.count <= 512 else { fail("编辑位置距文末过远，未移动光标或调用模型"); return }
            key("caret_from_end", count: tail.count, next: "context_caret")
        case "context_caret":
            let probe = client.probe()
            guard pending, generation == token, let original = contextOriginal,
                  probe.selection == NSRange(location: (contextPrefix as NSString).length, length: 0),
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else { return }
            preparedEditContext = PreparedEditContext(original: original, dictatedText: contextWhole, caret: probe.selection)
            verifiedDocument = contextWhole
            readbackDiagnostics.merge(["context_characters": (contextWhole as NSString).length,
                "context_prepare_ms": (clock() - operationStartedAt) * 1000, "ack_source": "native_selection"]) { _, new in new }
            pending = false; stage = ""
        case "native_written", "native_caret", "native_restore_caret":
            let probe = client.probe()
            let observed = replacementTextVerified ? result : client.readText(in: expectedRange)
            guard pending, generation == token else { return }
            let numbers: (NSRange) -> [Int] = { isValidRange($0) ? [$0.location, $0.length] : [-1, -1] }
            readbackDiagnostics = ["expected_characters": (result as NSString).length,
                "observed_characters": observed.map { ($0 as NSString).length } ?? -1,
                "text_matches": observed == result, "expected_selection": [requestedCaret ?? resultCaret, 0],
                "observed_selection": numbers(probe.selection), "expected_marked": [-1, -1],
                "observed_marked": numbers(probe.markedRange)]
            guard pending, generation == token, observed == result,
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else { return }
            replacementTextVerified = true
            if stage == "native_written" { key("caret_to_end", next: "native_caret") }
            else { confirmNativeCaret(probe) }
        case "sentence_selected":
            let probe = client.probe()
            guard probe.selection == expectedRange,
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                  client.readText(in: expectedRange) == expectedBefore else { return }
            if replacement.isEmpty { key("delete", next: "sentence_written") }
            else {
                client.commit(replacement)
                stage = "sentence_written"; keyAcknowledged = true; earliest = clock() + 0.04
            }
        case "sentence_written":
            let probe = client.probe()
            guard probe.selection == NSRange(location: resultCaret, length: 0),
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                  client.readText(in: NSRange(location: expectedRange.location, length: (replacement as NSString).length)) == replacement else { return }
            pending = false; stage = ""
        case "capture":
            guard let whole = selectedDocument(), isValidRange(expectedRange, length: (whole as NSString).length) else { return }
            guard pending, generation == token else { return }
            guard (whole as NSString).substring(with: expectedRange) == expectedBefore else {
                fail("输入框原文已变化，未执行替换"); return
            }
            result = (whole as NSString).replacingCharacters(in: expectedRange, with: replacement)
            resultCaret = requestedCaret ?? (selectBeforeReplacing ? (result as NSString).length :
                expectedRange.location + (replacement as NSString).length)
            let prefix = (result as NSString).substring(to: resultCaret)
            let tail = (result as NSString).substring(from: resultCaret)
            guard prefix.count + tail.count == result.count, tail.count <= 512 else {
                fail("输入框待恢复光标离文末过远，未修改文字"); return
            }
            writeSelected(whole)
        case "written":
            if selectBeforeReplacing {
                let probe = client.probe()
                let observed = client.readText(in: NSRange(location: 0, length: (result as NSString).length))
                guard pending, generation == token else { return }
                let wholeMatches = observed == result
                readbackDiagnostics = ["expected_characters": (result as NSString).length,
                    "observed_characters": observed.map { ($0 as NSString).length } ?? -1,
                    "text_matches": wholeMatches, "ack_source": wholeMatches ? "text" : "selection_collapse",
                    "operation_ms": (clock() - operationStartedAt) * 1000,
                    "post_write_ms": (clock() - selectionWrittenAt) * 1000]
                // A native selection was verified before a single insertText.
                // If the editor drops its document cache after that write,
                // selection collapse at the result end acknowledges delivery.
                // A dropped write leaves the old nonempty selection in place.
                let caret = NSRange(location: (result as NSString).length, length: 0)
                guard selectionWriteSent,
                      !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                      wholeMatches || (observed == nil && probe.selection == caret) else { return }
                replacementTextVerified = true
                if probe.selection == NSRange(location: resultCaret, length: 0) {
                    verifiedReplacement = requestedReplacement
                    verifiedReplacementCaret = probe.selection
                    verifiedDocument = result
                    pending = false; stage = ""
                } else { moveCaret() }
                return
            }
            guard client.readText(in: NSRange(location: 0, length: (result as NSString).length)) == result,
                  client.selection() == NSRange(location: (result as NSString).length, length: 0) else { return }
            moveCaret()
        case "verify":
            let probe = client.probe()
            if selectBeforeReplacing, replacementTextVerified {
                guard probe.selection == NSRange(location: resultCaret, length: 0),
                      !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else { return }
                verifiedReplacement = requestedReplacement
                verifiedReplacementCaret = probe.selection
                verifiedDocument = result
                pending = false; stage = ""; return
            }
            guard probe.selection == NSRange(location: resultCaret, length: 0),
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                  client.readText(in: NSRange(location: 0, length: (result as NSString).length)) == result,
                  client.readText(in: NSRange(location: (result as NSString).length, length: 1)) == nil,
                  client.readText(in: NSRange(location: (result as NSString).length, length: 2)) == nil else { return }
            pending = false; stage = ""
        default: break
        }
    }
}
