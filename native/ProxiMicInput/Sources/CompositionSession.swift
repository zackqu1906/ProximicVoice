import Foundation

let unspecifiedRange = NSRange(location: NSNotFound, length: NSNotFound)

func isValidRange(_ range: NSRange, length: Int? = nil) -> Bool {
    guard range.location != NSNotFound, range.length != NSNotFound,
          range.location >= 0, range.length >= 0,
          range.location <= Int.max - range.length else { return false }
    return length.map { NSMaxRange(range) <= $0 } ?? true
}

struct EditorTextRange {
    let text: String
    let range: NSRange
}

struct PreparedEditContext {
    let original: EditorSnapshot
    let dictatedText: String
    let caret: NSRange
}

struct EditorSnapshot {
    let text: String?
    let selection: NSRange
    let markedRange: NSRange
    let selectedText: String?
    let documentAccess: Bool
    var readableContext: EditorTextRange? = nil
    var complete: Bool { text != nil && isValidRange(selection, length: (text! as NSString).length) }
    var startError: String? {
        guard isValidRange(selection) else { return "请先点入可编辑的文本框" }
        return nil
    }
}

/// Immutable undo data only: no client, callbacks, timers or pending ASR work.
/// Dictation retains its inserted/replaced span, never a full document copy.
struct CompositionUndoRecord {
    let sourceUtteranceID: String
    let original: EditorSnapshot
    let raw: String
    let selection: NSRange
    let editedText: String?
    let retainedUnits: Int
    var committedRange: NSRange? = nil
}

/// A new ASR host never inherits an old host's transaction identifier or work.
/// Removing the session before interruption also suppresses stale IPC callbacks.
final class CompositionSessionLifetime {
    var current: CompositionSession?
    func clear(_ reason: String) {
        let previous = current
        current = nil
        previous?.interrupt(reason)
    }
}

struct CompositionProbe {
    let selection: NSRange
    let markedRange: NSRange
    let markedText: String?
    // Preserve the independent coordinate before adapter normalization.
    var reportedSelection: NSRange? = nil
}

protocol CompositionClient: AnyObject {
    func snapshot() throws -> EditorSnapshot
    func snapshotForBeginning() throws -> EditorSnapshot
    func probe() -> CompositionProbe
    func selection() -> NSRange
    func mark(_ text: String)
    func commit(_ text: String)
    func replace(_ range: NSRange, with text: String)
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?)
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?, expected: String)
    var preparesEditContext: Bool { get }
    func prepareEditContext(instruction: String, selectedText: String, source: EditorTextRange?, candidate: String)
    func discardEditCandidate()
    var preparedEditContext: PreparedEditContext? { get }
    var verifiedReplacementCaret: NSRange? { get }
    var verifiedReplacement: EditorTextRange? { get }
    func undo(_ range: NSRange, with text: String, preferNative: Bool)
    var clientOperationPending: Bool { get }
    var clientOperationFailure: String? { get }
    var clientOperationDiagnostics: [String: Any] { get }
    var compositionTextReadbackReliable: Bool { get }
    func cancelClientOperation()
    /// Exact document-relative read. Nil never means an empty string.
    func readText(in range: NSRange) -> String?
    func discardContextHint()
}

extension CompositionClient {
    func snapshotForBeginning() throws -> EditorSnapshot {
        let value = try snapshot()
        return EditorSnapshot(text: value.text, selection: value.selection,
            markedRange: unspecifiedRange, selectedText: value.selectedText,
            documentAccess: value.documentAccess, readableContext: value.readableContext)
    }
    func discardContextHint() {}
    var preparesEditContext: Bool { false }
    func prepareEditContext(instruction: String, selectedText: String, source: EditorTextRange?, candidate: String) {}
    func discardEditCandidate() {}
    var preparedEditContext: PreparedEditContext? { nil }
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?, expected: String) {
        replaceEditing(range, with: text, restoringCaret: restoringCaret)
    }
    func replaceEditing(_ range: NSRange, with text: String, restoringCaret: Int?) { replace(range, with: text) }
    var verifiedReplacementCaret: NSRange? { nil }
    var verifiedReplacement: EditorTextRange? { nil }
    var clientOperationPending: Bool { false }
    var clientOperationFailure: String? { nil }
    var clientOperationDiagnostics: [String: Any] { [:] }
    var compositionTextReadbackReliable: Bool { true }
    func cancelClientOperation() {}
    func undo(_ range: NSRange, with text: String, preferNative: Bool) { replace(range, with: text) }
    func readText(in range: NSRange) -> String? {
        guard let snapshot = try? snapshot(), let text = snapshot.text,
              isValidRange(range, length: (text as NSString).length) else { return nil }
        return (text as NSString).substring(with: range)
    }
}

enum CompositionError: Error, LocalizedError {
    case interrupted(String)
    case unsupported(String)
    case unconfirmed(String)
    var errorDescription: String? {
        switch self {
        case .interrupted(let s), .unsupported(let s), .unconfirmed(let s): return s
        }
    }
}

/// This owns a single sentence. All offsets are UTF-16, exactly as in IMKTextInput.
/// Its client is only called on the input method's main thread.
final class CompositionSession {
    enum Phase: String {
        case listening, finishing, dictated, editing, edited, undone, interrupted, error
    }
    let utteranceID: String
    private(set) var original: EditorSnapshot
    private let client: CompositionClient
    private(set) var phase: Phase = .listening
    private(set) var raw = ""
    private(set) var revision = 0
    private(set) var editRequested = false
    private(set) var error = ""
    private(set) var lastSequence: Int
    private(set) var hasComposition = false
    private(set) var hasModifiedText = false
    private(set) var replacementVerified = false
    private(set) var documentVerified = false
    private(set) var committedRangeVerified = false
    private(set) var committedRange: NSRange?
    private(set) var instructionRemoved = false
    private var expectedEditText: String?
    private var freshEditContext = false
    private var nativeUndoEligible = true
    private(set) var expectedSelection: NSRange
    private(set) var expectedText: String?
    private(set) var expectedMarked: NSRange = unspecifiedRange
    private(set) var lastAppliedText: String?
    private struct PendingReadback {
        let deadline: TimeInterval
        let stage: String
        let check: () throws -> Void
        let continuation: () -> Void
        var failure: Error
    }
    private enum DeferredCommand {
        case update(String, Bool), failure(String), convert, cancel
        case edit(String, Int, String?)
    }
    private let clock: () -> TimeInterval
    private var pendingReadback: PendingReadback?
    private var deferredCommands: [DeferredCommand] = []
    private var finalReceived = false
    private var cancelPending = false
    private var workGeneration = 0
    private var sendingNativeWrite = false
    private enum CompositionSelectionStyle { case unknown, standard, anchor }
    private var compositionSelectionStyle: CompositionSelectionStyle = .unknown
    // Keep sent ownership separate from readback-confirmed expectedMarked: the
    // very first mark can succeed even when its acknowledgement later fails.
    private struct SentComposition {
        var range: NSRange
        let text: String
        var commitSent = false
    }
    private var sentComposition: SentComposition?
    var awaitingReadback: Bool { pendingReadback != nil }
    var readbackStage: String { pendingReadback?.stage ?? "" }
    private(set) var readbackDiagnostics: [String: Any] = [:]
    var onFinishAudio: (() -> Void)?
    var onEdit: ((_ original: String, _ instruction: String, _ revision: Int) -> Void)?
    var onInterrupted: ((String) -> Void)?
    var onSettled: ((_ phase: String, _ text: String) -> Void)?
    var onChanged: (() -> Void)?

    init(client: CompositionClient, utteranceID: String, sequence: Int,
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) throws {
        self.client = client
        self.utteranceID = utteranceID
        self.lastSequence = sequence
        self.clock = clock
        original = try client.snapshotForBeginning()
        if let reason = original.startError { throw CompositionError.unsupported(reason) }
        expectedSelection = original.selection
        expectedText = original.text
        documentVerified = original.complete && original.documentAccess
        expectedEditText = editOriginal?.text
    }

    // Restoring history performs no client reads. cancel() performs the same
    // exact ownership/readback checks as the most recent sentence.
    init(client: CompositionClient, undo record: CompositionUndoRecord,
         utteranceID: String, sequence: Int, revision: Int,
         clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.client = client
        self.utteranceID = utteranceID
        self.lastSequence = sequence
        self.revision = revision
        self.clock = clock
        original = record.original
        raw = record.raw
        expectedSelection = record.selection
        expectedText = nil
        expectedEditText = record.editedText
        lastAppliedText = record.editedText
        phase = record.editedText == nil ? .dictated : .edited
        finalReceived = true
        hasModifiedText = true
        committedRangeVerified = record.editedText == nil
        committedRange = record.committedRange
        replacementVerified = record.editedText != nil
        instructionRemoved = record.editedText != nil
        nativeUndoEligible = false
    }

    func undoRecord(sourceUtteranceID: String? = nil) -> CompositionUndoRecord? {
        guard !awaitingReadback, !client.clientOperationPending, !hasComposition,
              original.documentAccess, original.selectedText != nil,
              (phase == .dictated && hasModifiedText && (committedRangeVerified || canReplace)) ||
              (phase == .edited && replacementVerified) else { return nil }
        let edited = phase == .edited ? lastAppliedText : nil
        let context = edited == nil ? nil : editOriginal
        if phase == .edited && (edited == nil || context == nil) { return nil }
        let compact = EditorSnapshot(text: nil, selection: original.selection,
                                     markedRange: unspecifiedRange, selectedText: original.selectedText,
                                     documentAccess: true, readableContext: context)
        let units = (raw as NSString).length + ((original.selectedText ?? "") as NSString).length +
            ((edited ?? "") as NSString).length + (context?.range.length ?? 0)
        return CompositionUndoRecord(sourceUtteranceID: sourceUtteranceID ?? utteranceID,
                                     original: compact, raw: raw, selection: expectedSelection,
                                     editedText: edited, retainedUnits: units, committedRange: edited == nil ? committedRange : nil)
    }

    var active: Bool { ![.undone, .interrupted, .error].contains(phase) }
    var canEdit: Bool {
        active && hasComposition && !raw.isEmpty && !finalReceived &&
            [.listening, .finishing].contains(phase) && !editRequested
    }
    var editOriginal: EditorTextRange? {
        if original.complete, let text = original.text {
            return EditorTextRange(text: text, range: NSRange(location: 0, length: (text as NSString).length))
        }
        guard let context = original.readableContext, isValidRange(context.range),
              original.selection.location >= context.range.location,
              NSMaxRange(original.selection) <= NSMaxRange(context.range),
              (context.text as NSString).length == context.range.length else { return nil }
        return context
    }
    var editScope: String { editOriginal == nil ? "unavailable" : (original.complete ? "document" : "readable_range") }
    var editContextAvailable: Bool { original.documentAccess && editOriginal != nil }
    private var dictatedEditText: String? {
        guard let context = editOriginal else { return nil }
        let relative = NSRange(location: original.selection.location - context.range.location, length: original.selection.length)
        guard isValidRange(relative, length: (context.text as NSString).length) else { return nil }
        return (context.text as NSString).replacingCharacters(in: relative, with: raw)
    }
    var cancelLabel: String {
        switch phase {
        case .listening, .finishing, .editing: return "取消"
        case .edited: return "还原听写"
        default: return "撤销"
        }
    }

    var canCancel: Bool {
        guard active else { return false }
        if [.finishing, .editing, .edited].contains(phase), editRequested || phase != .finishing { return true }
        return !hasComposition ? (!hasModifiedText || canReplace || committedRangeVerified) : original.selectedText != nil
    }
    var canReplace: Bool { original.complete && original.documentAccess && documentVerified }
    var dictatedText: String? {
        guard let text = original.text, isValidRange(original.selection, length: (text as NSString).length) else { return nil }
        return (text as NSString).replacingCharacters(in: original.selection, with: raw)
    }

    func accepts(sequence: Int) -> Bool {
        guard sequence > lastSequence else { return false }
        lastSequence = sequence
        return true
    }

    private func assertOwnership() throws {
        let now = try client.snapshot()
        recordReadback(expected: expectedText, observed: now.text, selection: now.selection, marked: now.markedRange)
        if let expected = expectedText {
            guard now.text == expected else { throw CompositionError.interrupted("输入框内容已变化，本句已停止写入") }
        }
        if isValidRange(expectedSelection), now.selection != expectedSelection {
            throw CompositionError.interrupted("光标或选区已变化，本句已结束")
        }
        if hasComposition, isValidRange(expectedMarked), now.markedRange != expectedMarked {
            throw CompositionError.interrupted("输入组合已由应用接管")
        }
    }

    private func acceptsLiveComposition(_ probe: CompositionProbe) -> Bool {
        guard !client.compositionTextReadbackReliable, !original.complete,
              hasComposition, !raw.isEmpty, isValidRange(probe.markedRange),
              probe.markedRange.length == (raw as NSString).length else { return false }
        let range = probe.markedRange
        let selection = probe.reportedSelection ?? probe.selection
        // A normalized whole-mark selection derives from markedRange itself;
        // it is not independent evidence that the coordinate origin changed.
        if compositionSelectionStyle != .standard, selection == NSRange(location: range.location, length: 0) { return true }
        return compositionSelectionStyle != .anchor &&
            (selection == range || selection == NSRange(location: NSMaxRange(range), length: 0))
    }

    private func adoptComposition(_ probe: CompositionProbe) {
        expectedSelection = probe.selection
        expectedMarked = probe.markedRange
        if sentComposition?.text == raw { sentComposition?.range = probe.markedRange }
    }

    private func assertCompositionOwnership() throws {
        let now = client.probe()
        recordReadback(expected: hasComposition ? raw : nil, observed: now.markedText,
                       selection: now.selection, marked: now.markedRange)
        if now.markedRange != expectedMarked, acceptsLiveComposition(now) { adoptComposition(now) }
        if client.compositionTextReadbackReliable, isValidRange(expectedSelection), !ownsSelection(now.selection) {
            throw CompositionError.interrupted("光标或选区已变化，本句已结束")
        }
        if hasComposition {
            if isValidRange(expectedMarked), now.markedRange != expectedMarked {
                throw CompositionError.interrupted("输入组合已由应用接管")
            }
            if client.compositionTextReadbackReliable, let text = now.markedText, text != raw {
                throw CompositionError.interrupted("未定稿文字已由应用修改")
            }
        }
    }

    private func acknowledgeComposition(start: Int?) throws {
        let now = client.probe()
        let desired = start.map { NSRange(location: $0, length: (raw as NSString).length) }
        recordReadback(expected: raw, observed: now.markedText, selection: now.selection, marked: now.markedRange,
                       desiredSelection: desired.map { NSRange(location: NSMaxRange($0), length: 0) }, desiredMarked: desired)
        if isValidRange(now.markedRange), now.markedRange.length != (raw as NSString).length {
            throw CompositionError.unconfirmed("输入框未确认完整的未定稿文字")
        }
        if client.compositionTextReadbackReliable, let text = now.markedText, text != raw {
            throw CompositionError.unconfirmed("输入框未确认本次未定稿文字更新")
        }
        let liveStart = acceptsLiveComposition(now) ? now.markedRange.location : start
        if let start = liveStart {
            let desired = NSRange(location: start, length: (raw as NSString).length)
            if (isValidRange(now.markedRange) || !client.compositionTextReadbackReliable), now.markedRange != desired,
               !raw.isEmpty {
                throw CompositionError.unconfirmed("输入框尚未确认未定稿文字的位置")
            }
            if client.compositionTextReadbackReliable {
                let endCaret = NSRange(location: NSMaxRange(desired), length: 0)
                let anchor = NSRange(location: start, length: 0)
                // Some clients keep selectedRange at the document insertion anchor
                // while composing. Learn that convention only from the first ACK,
                // with exact positive marked-text and range readback. A later mouse
                // move cannot change an already confirmed client convention.
                let exactMarkedText = now.markedRange == desired &&
                    (!client.compositionTextReadbackReliable || now.markedText == raw)
                let anchoredSelection = compositionSelectionStyle != .standard && exactMarkedText && now.selection == anchor
                let standardSelection = compositionSelectionStyle != .anchor &&
                    (now.selection == endCaret || (now.markedRange == desired && now.selection == desired))
                // Clearing a preedit can make markedRange/text unavailable. At
                // zero length the anchor and end caret coincide; no marked text
                // is needed to acknowledge that specific, already-owned removal.
                let clearedComposition = raw.isEmpty &&
                    (!isValidRange(now.markedRange) || now.markedRange.length == 0) &&
                    (now.markedText == nil || now.markedText == "") && now.selection == anchor
                guard anchoredSelection || standardSelection || clearedComposition else {
                    throw CompositionError.unconfirmed("输入框尚未确认未定稿文字的光标位置")
                }
                if compositionSelectionStyle == .unknown, !raw.isEmpty {
                    compositionSelectionStyle = anchoredSelection ? .anchor : .standard
                }
                if compositionSelectionStyle == .anchor {
                    recordReadback(expected: raw, observed: now.markedText, selection: now.selection, marked: now.markedRange,
                                   desiredSelection: anchor, desiredMarked: desired)
                }
            }
        }
        expectedText = dictatedText
        adoptComposition(now)
    }

    private func acknowledge(expected: String?, caret: Int?, alternateCaret: Int? = nil, allowUnverifiedDocument: Bool = false) throws {
        let now = try client.snapshot()
        recordReadback(expected: expected, observed: now.text, selection: now.selection, marked: now.markedRange,
                       desiredSelection: caret.map { NSRange(location: $0, length: 0) }, desiredMarked: unspecifiedRange)
        if isValidRange(now.markedRange), now.markedRange.length > 0 {
            throw CompositionError.unconfirmed("输入框尚未确认文字已定稿")
        }
        if let caret, isValidRange(now.selection), now.selection != NSRange(location: caret, length: 0),
           alternateCaret.map({ now.selection != NSRange(location: $0, length: 0) }) ?? true {
            throw CompositionError.unconfirmed("输入框尚未确认本次文字更新后的光标位置")
        }
        if allowUnverifiedDocument {
            // Native dictation commits only our marked range. An app that
            // cannot expose the surrounding document may still finish typing,
            // but must not gain whole-document edit powers. Local undo has
            // its own exact-range verification below.
            documentVerified = original.complete && original.documentAccess && expected != nil && now.text == expected
            let range = NSRange(location: original.selection.location, length: (raw as NSString).length)
            committedRangeVerified = original.documentAccess && original.selectedText != nil &&
                isValidRange(range) && now.selection == NSRange(location: NSMaxRange(range), length: 0) &&
                client.readText(in: range) == raw
        } else if let expected, now.text != expected {
            throw CompositionError.unconfirmed("输入框未确认本次文字更新，已停止后续写入")
        }
        expectedText = expected ?? now.text
        expectedSelection = now.selection
        expectedMarked = unspecifiedRange
        sentComposition = nil
    }

    /// Settle an owned composition from a coherent client receipt. The draft
    /// range and the committed document can use different coordinate origins;
    /// the original context snapshot is not a persistent document position.
    private func acknowledgeOwnedCommit(_ text: String, draftOrigin: Int?, expected: String?, allowUnverifiedDocument: Bool) throws {
        let generation = workGeneration
        let now = try client.snapshot()
        guard !isValidRange(now.markedRange) || now.markedRange.length == 0 else {
            throw CompositionError.unconfirmed("输入框尚未确认文字已定稿")
        }
        let length = (text as NSString).length
        let requireCaret = !allowUnverifiedDocument || client.compositionTextReadbackReliable
        let usableCaret = isValidRange(now.selection) && now.selection.length == 0 && now.selection.location >= length
        if requireCaret && !usableCaret {
            throw CompositionError.unconfirmed("输入框尚未提供定稿后的光标位置")
        }
        let range = usableCaret ? NSRange(location: now.selection.location - length, length: length) : unspecifiedRange
        let draftRange = draftOrigin.map { NSRange(location: $0, length: length) }
        let observed = usableCaret ? client.readText(in: range) : nil
        let confirmed = observed == text
        recordReadback(expected: text, observed: observed, selection: now.selection, marked: now.markedRange,
                       desiredSelection: draftRange.map { NSRange(location: NSMaxRange($0), length: 0) },
                       desiredMarked: unspecifiedRange)
        let originChanged = !usableCaret || (draftRange.map { $0 != range } ?? false)
        var confirmedRange = usableCaret && confirmed
        if originChanged && !text.isEmpty {
            // An earlier identical sentence under a moved caret is ambiguous
            // when the just-written span is still readable at its old address.
            confirmedRange = confirmedRange && draftRange.map { client.readText(in: $0) == nil } == true
            if requireCaret && !confirmedRange {
                throw CompositionError.unconfirmed("输入框尚未确认定稿文字的位置")
            }
        }
        if !allowUnverifiedDocument, let expected, now.text != expected {
            throw CompositionError.unconfirmed("输入框未确认本次文字更新，已停止后续写入")
        }
        let live = client.probe()
        guard active, workGeneration == generation, (!requireCaret || live.selection == now.selection),
              !isValidRange(live.markedRange) || live.markedRange.length == 0 else {
            throw CompositionError.unconfirmed("输入框定稿状态正在更新")
        }
        // Keep a separate committed span, including in history. Never rewrite
        // the original snapshot to pretend it used the new coordinate system.
        // A stale/missing caret must not fail native dictation after the mark
        // ended. Unproven coordinates also must not authorize a later deletion.
        committedRange = allowUnverifiedDocument && text == raw && confirmedRange && live.selection == now.selection &&
            original.documentAccess && original.selectedText != nil ? range : nil
        committedRangeVerified = committedRange != nil
        documentVerified = original.complete && original.documentAccess && expected != nil && now.text == expected
        expectedText = originChanged ? now.text : (expected ?? now.text)
        expectedSelection = live.selection
        expectedMarked = unspecifiedRange
        sentComposition = nil
        if originChanged { client.discardContextHint() }
        readbackDiagnostics["ack_source"] = confirmedRange ? "committed_range" : "composition_end"
        readbackDiagnostics["caret_required"] = requireCaret
    }

    private func recordReadback(expected: String?, observed: String?, selection: NSRange, marked: NSRange,
                                desiredSelection: NSRange? = nil, desiredMarked: NSRange? = nil) {
        func numbers(_ range: NSRange) -> [Int] { isValidRange(range) ? [range.location, range.length] : [-1, -1] }
        readbackDiagnostics = ["expected_characters": expected.map { ($0 as NSString).length } ?? -1,
                               "observed_characters": observed.map { ($0 as NSString).length } ?? -1,
                               "text_matches": expected == observed,
                               "expected_selection": numbers(desiredSelection ?? expectedSelection),
                               "observed_selection": numbers(selection),
                               "expected_marked": numbers(desiredMarked ?? expectedMarked),
                               "observed_marked": numbers(marked)]
    }

    // AppKit clients backed by another process may return a stale read directly
    // after a successful write. Retry only the read on later run-loop ticks.
    private func awaitReadback(_ stage: String, check: @escaping () throws -> Void,
                               then continuation: @escaping () -> Void) {
        let generation = workGeneration
        let clientOperation = ["replace", "undo_replace", "edit_context"].contains(stage)
        let checked = { [unowned self] in
            if clientOperation {
                if !client.clientOperationDiagnostics.isEmpty { readbackDiagnostics = client.clientOperationDiagnostics }
                if let failure = client.clientOperationFailure { throw CompositionError.unconfirmed(failure) }
                if client.clientOperationPending { throw CompositionError.unconfirmed("正在等待输入框完成操作") }
            }
            try check()
        }
        do {
            try checked()
            guard workGeneration == generation, active || (phase == .error && stage == "recovery") else { return }
            continuation()
        } catch {
            guard workGeneration == generation, active || (phase == .error && stage == "recovery") else { return }
            let liveGeometry = !client.compositionTextReadbackReliable &&
                ["mark", "composition_ownership", "selection_ownership"].contains(stage)
            let timeout = client.clientOperationPending ? 3.0 : (liveGeometry ? 1.0 : 0.5)
            pendingReadback = PendingReadback(deadline: clock() + timeout, stage: stage,
                                              check: checked, continuation: continuation, failure: error)
            onChanged?()
        }
    }

    func advanceReadback() {
        if phase == .error, pendingReadback == nil {
            // After the bounded recovery window only observe user/system
            // removal. Never resume a write just because a late read matches.
            guard hasComposition else { return }
            if clearCompositionIfAbsent(client.probe()) { onChanged?() }
            return
        }
        guard var pending = pendingReadback,
              active || (phase == .error && pending.stage == "recovery") else { return }
        guard clock() < pending.deadline else {
            pendingReadback = nil
            if pending.stage == "recovery" { onChanged?() }
            else if pending.stage == "selection_ownership" {
                interrupt("输入位置持续变化，本句已结束", commitIfOwned: false)
            }
            else { stopOnError(pending.failure) }
            return
        }
        let generation = workGeneration
        do {
            try pending.check()
            guard workGeneration == generation else { return }
            pendingReadback = nil
            pending.continuation()
            onChanged?()
            drainDeferredCommands()
        } catch {
            guard workGeneration == generation else { return }
            pending.failure = error
            pendingReadback = pending
        }
    }

    private func withOwnership(_ continuation: @escaping () -> Void) {
        awaitReadback("ownership", check: { [unowned self] in try assertOwnership() }, then: continuation)
    }

    private func withCompositionOwnership(_ continuation: @escaping () -> Void) {
        if hasComposition || !client.compositionTextReadbackReliable {
            awaitReadback("composition_ownership", check: { [unowned self] in try assertCompositionOwnership() }, then: continuation)
        } else { withOwnership(continuation) }
    }

    private func drainDeferredCommands() {
        while active, pendingReadback == nil, !deferredCommands.isEmpty {
            let command = deferredCommands.removeFirst()
            switch command {
            case .update(let text, let final): performUpdate(text, final: final)
            case .failure(let message): performASRFailure(message)
            case .convert: performConvert()
            case .cancel:
                cancelPending = false
                performCancel()
            case .edit(let text, let revision, let error): performEdit(text: text, revision: revision, error: error)
            }
        }
    }

    private var compositionStart: Int? {
        if isValidRange(expectedMarked) { return expectedMarked.location }
        return isValidRange(original.selection) ? original.selection.location : nil
    }

    private func ownsSelection(_ selection: NSRange) -> Bool {
        if hasComposition, compositionSelectionStyle == .anchor, isValidRange(expectedMarked) {
            return selection == NSRange(location: expectedMarked.location, length: 0)
        }
        if selection == expectedSelection { return true }
        guard hasComposition, isValidRange(expectedMarked) else { return false }
        return selection == expectedMarked || selection == NSRange(location: NSMaxRange(expectedMarked), length: 0)
    }

    private func commitOwned(_ text: String, expected: String?, allowUnverifiedDocument: Bool = false, then continuation: @escaping () -> Void) {
        guard active else { return }
        let origin = compositionStart
        let generation = workGeneration
        sentComposition?.commitSent = true
        sendNativeWrite { client.commit(text) }
        guard active, workGeneration == generation else { return }
        hasComposition = false
        awaitReadback("commit", check: { [unowned self] in try acknowledgeOwnedCommit(text, draftOrigin: origin, expected: expected, allowUnverifiedDocument: allowUnverifiedDocument) }, then: continuation)
    }

    /// Partial and final updates share the same composition; final never presses Return.
    func update(_ text: String, final: Bool) {
        guard [.listening, .finishing].contains(phase), !finalReceived, !cancelPending else { return }
        if final { finalReceived = true }
        if pendingReadback != nil {
            // Intermediate ASR revisions supersede each other, but never a final.
            if case .update(_, false)? = deferredCommands.last { deferredCommands.removeLast() }
            deferredCommands.append(.update(text, final))
            return
        }
        performUpdate(text, final: final)
    }

    private func performUpdate(_ text: String, final: Bool) {
        guard [.listening, .finishing].contains(phase), !cancelPending else { return }
        if final {
            withCompositionOwnership { [unowned self] in
                guard !cancelPending else { drainDeferredCommands(); return }
                writeUpdate(text, final: true)
            }
        } else {
            do { try assertCompositionOwnership(); writeUpdate(text, final: false) }
            catch { stopOnError(error) }
        }
    }

    private func writeUpdate(_ text: String, final: Bool) {
        guard active else { return }
        if text != raw || (!hasComposition && !text.isEmpty) {
            let start = compositionStart
            let generation = workGeneration
            sentComposition = start.map { SentComposition(range: NSRange(location: $0, length: (text as NSString).length), text: text) }
            sendNativeWrite { client.mark(text) }
            guard active, workGeneration == generation else { return }
            hasComposition = true
            hasModifiedText = true
            raw = text
            awaitReadback("mark", check: { [unowned self] in try acknowledgeComposition(start: start) }, then: { [unowned self] in finishUpdate(final: final) })
        } else { finishUpdate(final: final) }
    }

    private func finishUpdate(final: Bool) {
        guard active else { return }
        guard !cancelPending else { onChanged?(); drainDeferredCommands(); return }
        if final, hasComposition {
            withCompositionOwnership { [unowned self] in
                guard !cancelPending else { drainDeferredCommands(); return }
                if editRequested, original.selectedText != nil, canRemoveInstruction() {
                    // Keep the visible instruction until a model result exists.
                    // Cancelling while waiting simply commits this as dictation.
                    completeUpdate(final: true)
                } else {
                    commitOwned(raw, expected: dictatedText, allowUnverifiedDocument: true) { [unowned self] in completeUpdate(final: true) }
                }
            }
        } else { completeUpdate(final: final) }
    }

    private func canRemoveInstruction() -> Bool {
        if client.preparesEditContext {
            return hasComposition && isValidRange(expectedMarked) && original.selectedText != nil
        }
        guard editContextAvailable, let context = editOriginal, let dictated = dictatedEditText else { return false }
        if !client.compositionTextReadbackReliable {
            // The caller has just verified ownership of the live composition.
            // The IMK document cache need not contain that composition yet.
            // Removing our own mark is not a committed document replacement.
            // After the model returns, restore the original selection, then
            // verify the committed original before applying any model result.
            return hasComposition && isValidRange(expectedMarked) && original.selectedText != nil
        }
        // With only a readable window, insertion at its start can mimic a
        // successful replacement while leaving the old suffix behind. Without
        // whole-document proof, do not grant that ambiguous replacement.
        if !original.complete, context.range.length > 0,
           NSMaxRange(original.selection) == context.range.location { return false }
        let range = NSRange(location: context.range.location, length: (dictated as NSString).length)
        guard client.readText(in: range) == dictated else { return false }
        return !original.complete || (try? client.snapshot().text) == dictated
    }

    private func completeUpdate(final: Bool) {
        guard active else { return }
        error = ""
        if final {
            phase = .dictated
            if instructionRemoved, !editRequested { restoreDictation(); return }
            if editRequested, hasComposition { startEditing() }
            else if editRequested {
                editRequested = false
                error = "编辑原文校验未通过，未调用模型，已保留听写"
                if !cancelPending { onSettled?(phase.rawValue, raw) }
            }
            else if !cancelPending { onSettled?(phase.rawValue, raw) }
        }
        onChanged?()
        drainDeferredCommands()
    }

    func failASR(_ message: String) {
        guard [.listening, .finishing].contains(phase), !cancelPending else { return }
        finalReceived = true
        if pendingReadback != nil {
            deferredCommands = [.failure(message)]
            onFinishAudio?()
            return
        }
        performASRFailure(message)
    }

    private func performASRFailure(_ message: String) {
        withCompositionOwnership { [unowned self] in
            guard !cancelPending else { drainDeferredCommands(); return }
            let complete = { [unowned self] in
                revision += 1
                editRequested = false
                phase = .dictated
                error = message
                onFinishAudio?()
                if !cancelPending { onSettled?(phase.rawValue, raw) }
                onChanged?()
                drainDeferredCommands()
            }
            if hasComposition { commitOwned(raw, expected: dictatedText, allowUnverifiedDocument: true, then: complete) }
            else { complete() }
        }
    }

    func finish() {
        guard phase == .listening else { return }
        phase = .finishing
        onFinishAudio?()
        onChanged?()
    }

    func convert() {
        guard !cancelPending else { return }
        guard canEdit else {
            error = "请在文字还有下划线时右滑，确认本句为编辑指令"
            onChanged?()
            return
        }
        if [.listening, .finishing].contains(phase) {
            // Conversion ends audio immediately, even while a native write is
            // awaiting confirmation. The final cannot write until that ACK.
            error = ""
            editRequested = true
            phase = .finishing
            onFinishAudio?()
            onChanged?()
        }
    }

    private func performConvert() {
        convert()
    }

    private func startEditing() {
        guard editOriginal != nil, !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            editRequested = false
            restoreDictation(error: editOriginal == nil ? "当前输入框未提供可读的编辑原文，已保留听写" : "")
            return
        }
        revision += 1
        let requestedRevision = revision
        nativeUndoEligible = false
        phase = .editing
        withCompositionOwnership { [unowned self] in
            guard editRequested, !cancelPending, revision == requestedRevision else { drainDeferredCommands(); return }
            error = ""
            // Reading the initial IMK context is invisible. Treat it as a model
            // proposal only: native selection verifies the source after the
            // result arrives, before any candidate can replace the document.
            onEdit?(editOriginal?.text ?? "", raw, revision)
            onChanged?()
        }
    }

    private func prepareFreshEditContext(candidate: String, revision requestedRevision: Int) {
        // This runs only after a model result. Keep the spoken instruction
        // visible until the verified selection is replaced in one native write.
        let generation = workGeneration
        if hasComposition {
            if var sent = sentComposition { sent.commitSent = true; sentComposition = sent }
            sendNativeWrite { client.commit(raw) }
            guard active, workGeneration == generation else { return }
            hasComposition = false
            expectedMarked = unspecifiedRange
        }
        sendNativeWrite {
            client.prepareEditContext(instruction: raw, selectedText: original.selectedText ?? "",
                                      source: editOriginal, candidate: candidate)
        }
        guard active, workGeneration == generation else { return }
        awaitReadback("edit_context", check: { [unowned self] in
            guard let context = client.preparedEditContext else {
                throw CompositionError.unconfirmed("正在读取当前编辑原文")
            }
            original = context.original
            expectedEditText = context.dictatedText
            expectedText = nil
            expectedSelection = context.caret
            documentVerified = false
            freshEditContext = true
            committedRangeVerified = true
        }, then: { [unowned self] in
            guard editRequested, revision == requestedRevision else { drainDeferredCommands(); return }
            let resultRange = NSRange(location: 0, length: (candidate as NSString).length)
            if client.verifiedReplacement?.range == resultRange,
               client.verifiedReplacement?.text == candidate,
               let caret = client.verifiedReplacementCaret {
                expectedEditText = candidate
                expectedSelection = caret
                committedRangeVerified = false
                replacementVerified = true
                completeEdit(candidate)
            } else if cancelPending {
                drainDeferredCommands()
            } else {
                // Keep one model request per instruction. A real source mismatch
                // cannot safely reuse its result; preserve dictation instead of
                // starting another model wait with the native preedit removed.
                revision += 1
                editRequested = false
                restoreDictation(error: "编辑原文读取不一致，已保留听写")
            }
        })
    }

    private func completeEdit(_ text: String) {
        lastAppliedText = text
        instructionRemoved = true
        phase = .edited
        error = ""
        if !cancelPending { onSettled?(phase.rawValue, text) }
        onChanged?()
        drainDeferredCommands()
    }

    private func withEditOwnership(_ continuation: @escaping () -> Void) {
        awaitReadback("ownership", check: { [unowned self] in
            guard let context = editOriginal, let expected = expectedEditText else {
                throw CompositionError.unsupported("输入框未提供可校验的编辑原文")
            }
            let range = NSRange(location: context.range.location, length: (expected as NSString).length)
            let probe = client.probe()
            if client.preparesEditContext {
                // The native selection operation checks the original once at
                // the point of replacement. Do not query a stale IMK context
                // window again before that selection has refreshed it.
                guard probe.selection == expectedSelection,
                      !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else {
                    throw CompositionError.interrupted("编辑光标已变化，未执行替换")
                }
                return
            }
            let observed = client.readText(in: range)
            recordReadback(expected: expected, observed: observed, selection: probe.selection, marked: probe.markedRange)
            guard observed == expected, probe.selection == expectedSelection,
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else {
                throw CompositionError.interrupted("编辑原文或光标已变化，未执行替换")
            }
            // When full context was available, keep the stronger full-document
            // check as well (including changes beyond the old end of text).
            if original.complete { try assertOwnership() }
        }, then: continuation)
    }

    // NSTextView can preserve a caret in the suffix and shift it by the edit's
    // UTF-16 delta instead of moving it to the replaced range's end. This is a
    // predicted position from the preflight, never an arbitrary readback caret.
    private func suffixCaret(afterReplacing range: NSRange, with text: String) -> Int? {
        guard expectedSelection.length == 0, isValidRange(expectedSelection),
              expectedSelection.location >= NSMaxRange(range) else { return nil }
        return expectedSelection.location - range.length + (text as NSString).length
    }

    private func replaceEditScope(with text: String, restoring: Bool = false, then continuation: @escaping () -> Void) {
        guard editContextAvailable, let context = editOriginal, let before = expectedEditText else { return }
        withEditOwnership { [unowned self] in
            if cancelPending, !restoring { drainDeferredCommands(); return }
            let generation = workGeneration
            let range = NSRange(location: context.range.location, length: (before as NSString).length)
            let suffix = suffixCaret(afterReplacing: range, with: text)
            let restoredCaret = restoring ? original.selection.location + (raw as NSString).length : nil
            sendNativeWrite { client.replaceEditing(range, with: text, restoringCaret: restoredCaret, expected: before) }
            guard active, workGeneration == generation else { return }
            let result = NSRange(location: range.location, length: (text as NSString).length)
            awaitReadback("replace", check: { [unowned self] in
                let probe = client.probe()
                // The adapter already confirmed this exact write and its caret.
                // Re-reading the full document after caret motion can see a new
                // IMK context window, rather than the document just edited.
                let receipt = client.verifiedReplacement
                let verified = receipt?.range == result && receipt?.text == text
                let observed = verified ? text : client.readText(in: result)
                let caret = NSRange(location: NSMaxRange(result), length: 0)
                let positioned = client.verifiedReplacementCaret
                recordReadback(expected: text, observed: observed, selection: probe.selection,
                               marked: probe.markedRange, desiredSelection: positioned ?? caret, desiredMarked: unspecifiedRange)
                guard observed == text,
                      probe.selection == caret || positioned == probe.selection || suffix.map({ probe.selection == NSRange(location: $0, length: 0) }) == true,
                      !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else {
                    throw CompositionError.unconfirmed("输入框尚未确认编辑结果")
                }
                if original.complete && !verified {
                    guard try client.snapshot().text == text else {
                        throw CompositionError.unconfirmed("输入框尚未确认完整编辑结果")
                    }
                }
                expectedEditText = text
                expectedText = original.complete ? text : nil
                expectedSelection = probe.selection
                expectedMarked = unspecifiedRange
                documentVerified = original.complete && original.documentAccess
                committedRangeVerified = false
                replacementVerified = true
            }, then: continuation)
        }
    }

    private func restoreDictation(error message: String = "") {
        if freshEditContext, !instructionRemoved, lastAppliedText == nil {
            phase = .dictated
            editRequested = false
            error = message
            if !cancelPending { onSettled?(phase.rawValue, raw) }
            onChanged?()
            drainDeferredCommands()
            return
        }
        if hasComposition {
            withCompositionOwnership { [unowned self] in
                commitOwned(raw, expected: dictatedText, allowUnverifiedDocument: true) { [unowned self] in
                    phase = .dictated
                    instructionRemoved = false
                    error = message
                    if !cancelPending { onSettled?(phase.rawValue, raw) }
                    onChanged?()
                    drainDeferredCommands()
                }
            }
            return
        }
        guard let text = dictatedEditText else {
            stopOnError(CompositionError.unsupported("无法校验原听写内容，未执行恢复"))
            return
        }
        replaceEditScope(with: text, restoring: true) { [unowned self] in
            instructionRemoved = false
            phase = .dictated
            error = message
            let range = NSRange(location: original.selection.location, length: (raw as NSString).length)
            committedRangeVerified = original.documentAccess && original.selectedText != nil && client.readText(in: range) == raw
            if !cancelPending { onSettled?(phase.rawValue, raw) }
            onChanged?()
            drainDeferredCommands()
        }
    }

    private func replaceCommitted(range: NSRange, text: String, expected: String, then continuation: @escaping () -> Void) {
        guard canReplace else {
            stopOnError(CompositionError.unsupported("当前输入框不支持可靠的定稿文字恢复"))
            return
        }
        withOwnership { [unowned self] in
            // A model result still waiting for its preflight has not written
            // anything yet; a cancellation must win before that first write.
            if cancelPending, phase == .editing { drainDeferredCommands(); return }
            let generation = workGeneration
            let suffix = suffixCaret(afterReplacing: range, with: text)
            sendNativeWrite { client.undo(range, with: text, preferNative: nativeUndoEligible) }
            guard active, workGeneration == generation else { return }
            let caret = range.location + (text as NSString).length
            awaitReadback("replace", check: { [unowned self] in try acknowledge(expected: expected, caret: caret, alternateCaret: suffix) }, then: { [unowned self] in
                replacementVerified = true
                continuation()
            })
        }
    }

    func applyEdit(text: String, revision incomingRevision: Int, error incomingError: String?) {
        guard phase == .editing, revision == incomingRevision, !cancelPending else { return }
        if pendingReadback != nil {
            // A second delivery of the same model result must not issue another write.
            if !deferredCommands.contains(where: { if case .edit = $0 { return true }; return false }), readbackStage != "replace" {
                deferredCommands.append(.edit(text, incomingRevision, incomingError))
            }
        } else { performEdit(text: text, revision: incomingRevision, error: incomingError) }
    }

    private func performEdit(text: String, revision incomingRevision: Int, error incomingError: String?) {
        guard phase == .editing, revision == incomingRevision, !cancelPending else { return }
        if let incomingError, !incomingError.isEmpty {
            revision += 1
            editRequested = false
            restoreDictation(error: incomingError)
            return
        }
        let apply = { [unowned self] in
            replaceEditScope(with: text) { [unowned self] in
                completeEdit(text)
            }
        }
        if client.preparesEditContext {
            if hasComposition {
                withCompositionOwnership { [unowned self] in
                    guard !cancelPending else { drainDeferredCommands(); return }
                    prepareFreshEditContext(candidate: text, revision: incomingRevision)
                }
            } else {
                withEditOwnership { [unowned self] in
                    guard !cancelPending else { drainDeferredCommands(); return }
                    prepareFreshEditContext(candidate: text, revision: incomingRevision)
                }
            }
            return
        }
        if hasComposition {
            awaitReadback("ownership", check: { [unowned self] in
                try assertCompositionOwnership()
                guard canRemoveInstruction(), original.selectedText != nil else {
                    throw CompositionError.unconfirmed("原文或指令尚未确认，未应用编辑结果")
                }
            }, then: { [unowned self] in
                guard !cancelPending, let selected = original.selectedText else { drainDeferredCommands(); return }
                commitOwned(selected, expected: original.text, allowUnverifiedDocument: true) { [unowned self] in
                    instructionRemoved = true
                    expectedEditText = editOriginal?.text
                    apply()
                }
            })
        } else {
            apply()
        }
    }

    func cancel() {
        guard active else { return }
        if phase == .dictated, hasModifiedText, !canReplace, !committedRangeVerified {
            error = "当前输入框无法校验本句文字，暂不能撤销"
            onChanged?()
            return
        }
        if phase == .finishing && editRequested {
            editRequested = false
            revision += 1
            onChanged?()
            return
        }
        if phase == .editing, pendingReadback == nil || readbackStage == "ownership" {
            // No edit has written yet. Drop both the model revision and any
            // read-only preflight immediately, then restore the dictated text.
            clearDeferredWork()
            revision += 1
            editRequested = false
            restoreDictation()
            return
        }
        if pendingReadback != nil {
            guard !cancelPending else { return }
            if readbackStage == "edit_context" { client.discardEditCandidate() }
            cancelPending = true
            finalReceived = true
            deferredCommands = [.cancel]
            if [.listening, .finishing].contains(phase) { onFinishAudio?() }
            onChanged?()
            return
        }
        performCancel()
    }

    private func performCancel() {
        guard active else { return }
        if phase == .editing || phase == .edited {
            revision += 1
            editRequested = false
            restoreDictation()
            return
        }
        if phase == .dictated, hasModifiedText, !hasComposition, !canReplace, committedRangeVerified {
            undoCommittedRange()
            return
        }
        // Restoring the selection replaced by our live preedit only touches
        // that composition. Some clients cannot expose the full document
        // until the preedit ends, even though its range and text are readable.
        // Committed undo still goes through the whole-document check.
        withCompositionOwnership { [unowned self] in cancelOwned() }
    }

    private func undoCommittedRange() {
        guard let selected = original.selectedText, original.documentAccess else { return }
        let range = committedRange ?? NSRange(location: original.selection.location, length: (raw as NSString).length)
        awaitReadback("undo_ownership", check: { [unowned self] in
            let probe = client.probe()
            guard probe.selection == expectedSelection,
                  !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                  client.readText(in: range) == raw else {
                throw CompositionError.interrupted("本句文字或光标已变化，未执行撤销")
            }
        }, then: { [unowned self] in
            let generation = workGeneration
            let suffix = suffixCaret(afterReplacing: range, with: selected)
            sendNativeWrite { client.undo(range, with: selected, preferNative: nativeUndoEligible) }
            guard active, workGeneration == generation else { return }
            let restored = NSRange(location: range.location, length: (selected as NSString).length)
            awaitReadback("undo_replace", check: { [unowned self] in
                let probe = client.probe()
                let observed = client.readText(in: restored)
                recordReadback(expected: selected, observed: observed,
                               selection: probe.selection, marked: probe.markedRange,
                               desiredSelection: NSRange(location: NSMaxRange(restored), length: 0),
                               desiredMarked: unspecifiedRange)
                guard probe.selection == NSRange(location: NSMaxRange(restored), length: 0) ||
                      suffix.map({ probe.selection == NSRange(location: $0, length: 0) }) == true,
                      !isValidRange(probe.markedRange) || probe.markedRange.length == 0,
                      observed == selected else {
                    throw CompositionError.unconfirmed("输入框尚未确认本句撤销")
                }
            }, then: { [unowned self] in
                replacementVerified = true
                committedRangeVerified = false
                completeUndo()
            })
        })
    }

    private func cancelOwned() {
        do {
            error = ""
            if phase == .finishing && editRequested {
                // Audio remains ended. A late final is accepted as ordinary dictation.
                editRequested = false
                revision += 1
            } else if phase == .editing {
                revision += 1
                editRequested = false
                restoreDictation()
                return
            } else if phase == .edited {
                revision += 1
                editRequested = false
                restoreDictation()
                return
            } else {
                if hasComposition {
                    guard let selected = original.selectedText else {
                        throw CompositionError.unsupported("输入框未提供原选中文字，无法可靠撤销")
                    }
                    commitOwned(selected, expected: original.text) { [unowned self] in completeUndo() }
                    return
                } else if hasModifiedText {
                    guard let originalText = original.text, let selected = original.selectedText else {
                        throw CompositionError.unsupported("输入框未提供完整上下文，定稿后无法可靠撤销")
                    }
                    let speechRange = NSRange(location: original.selection.location, length: (raw as NSString).length)
                    replaceCommitted(range: speechRange, text: selected, expected: originalText) { [unowned self] in completeUndo() }
                    return
                }
                completeUndo()
            }
            onChanged?()
        } catch { stopOnError(error) }
    }

    private func completeUndo() {
        revision += 1
        editRequested = false
        error = ""
        phase = .undone
        clearDeferredWork()
        onFinishAudio?()
        onSettled?(phase.rawValue, "")
        onChanged?()
    }

    @discardableResult
    func commitCompositionRequested() -> Bool {
        if phase == .error, hasComposition || awaitingReadback {
            // A real client lifecycle request takes precedence over the
            // bounded recovery. It must never trigger another cleanup write.
            interrupt("应用结束了当前输入组合", commitIfOwned: false)
            return true
        }
        // A client may flush its input context before any speech text exists.
        // That callback commits marked text; it is not an audio stop request.
        guard active, hasComposition else { return false }
        interrupt("应用结束了当前输入组合")
        return true
    }

    /// User input or lifecycle termination invalidates all asynchronous work first.
    /// Composition is only committed when it is still demonstrably ours.
    func interrupt(_ reason: String, commitIfOwned: Bool = true) {
        guard active || (phase == .error && (hasComposition || awaitingReadback)) else { return }
        let wasActive = active
        let wasAwaiting = awaitingReadback
        clearDeferredWork()
        revision += 1
        editRequested = false
        // A client read/write can synchronously call back into IMK. Make the
        // transaction terminal before cleanup so those callbacks cannot revive
        // it or recursively issue another commit.
        phase = .interrupted
        if wasActive && hasComposition && commitIfOwned && !sendingNativeWrite,
           sentComposition?.commitSent != true {
            do {
                if wasAwaiting {
                    guard let sent = sentComposition, !sent.text.isEmpty,
                          ownsSentComposition(client.probe(), sent: sent) else {
                        throw CompositionError.interrupted("尚未确认当前输入组合归属")
                    }
                } else { try assertCompositionOwnership() }
                sentComposition?.commitSent = true
                sendNativeWrite { client.commit(raw) }
            } catch { /* Never overwrite a user takeover to clean up composition. */ }
        }
        hasComposition = false
        sentComposition = nil
        error = reason
        onFinishAudio?()
        onInterrupted?(reason)
        onChanged?()
    }

    func checkSelection() {
        guard active, !awaitingReadback, isValidRange(expectedSelection) else { return }
        if !client.compositionTextReadbackReliable {
            // A polled coordinate is not a user-input event. The remote client
            // can briefly regress to an old or unavailable range after an ACK.
            // Only the live marked range participates in ownership checks.
            // Completed text is validated again when an undo/edit is requested;
            // polling a cached caret must not retire a successful dictation.
            guard hasComposition else { return }
            awaitReadback("selection_ownership", check: { [unowned self] in
                try assertCompositionOwnership()
            }, then: {})
            return
        }
        let selection = client.selection()
        if isValidRange(selection), !ownsSelection(selection) {
            interrupt("光标或选区已变化，本句已结束", commitIfOwned: false)
        }
    }

    private func stopOnError(_ problem: Error) {
        guard active else { return }
        clearDeferredWork()
        if case CompositionError.unsupported(let message) = problem {
            error = message
            onChanged?()
            return
        }
        revision += 1
        editRequested = false
        phase = .error
        error = problem.localizedDescription
        beginErrorRecovery()
        onFinishAudio?()
        onInterrupted?(error)
        onChanged?()
    }

    private func clearCompositionIfAbsent(_ probe: CompositionProbe) -> Bool {
        guard !isValidRange(probe.markedRange) || probe.markedRange.length == 0 else { return false }
        hasComposition = false
        sentComposition = nil
        expectedMarked = unspecifiedRange
        return true
    }

    private func beginErrorRecovery() {
        guard sentComposition != nil else { return }
        // Error remains terminal for ASR and models. This is only a bounded,
        // one-time native commit of text proven to be our already-visible mark.
        awaitReadback("recovery", check: { [unowned self] in try recoverVisibleComposition() }, then: {})
    }

    private func recoverVisibleComposition() throws {
        let generation = workGeneration
        let probe = client.probe()
        guard phase == .error, workGeneration == generation else { return }
        if clearCompositionIfAbsent(probe) { return }
        guard let sent = sentComposition else {
            throw CompositionError.unconfirmed("当前输入组合无法确认归属，请手动完成或取消")
        }
        hasComposition = true
        recordReadback(expected: sent.text, observed: probe.markedText,
                       selection: probe.selection, marked: probe.markedRange,
                       desiredMarked: sent.range)
        // A prior commit may have completed with stale readback. Replaying it
        // would insert the same text twice once the actual mark has disappeared.
        guard !sent.commitSent else {
            throw CompositionError.unconfirmed("正在等待上一句输入组合结束")
        }
        guard ownsSentComposition(probe, sent: sent) else {
            throw CompositionError.unconfirmed("当前输入组合无法确认归属，请手动完成或取消")
        }
        sentComposition?.commitSent = true
        sendNativeWrite { client.commit(sent.text) }
        guard phase == .error, workGeneration == generation else { return }
        // No replacement, empty-string clearing, caret adjustment, or repeated
        // commit is used to force cleanup. Only confirm that this mark ended.
        if !clearCompositionIfAbsent(client.probe()) {
            throw CompositionError.unconfirmed("正在等待上一句输入组合结束")
        }
    }

    private func clearDeferredWork() {
        client.cancelClientOperation()
        workGeneration += 1
        pendingReadback = nil
        deferredCommands.removeAll()
        cancelPending = false
    }

    private func sendNativeWrite(_ operation: () -> Void) {
        let wasSending = sendingNativeWrite
        sendingNativeWrite = true
        defer { sendingNativeWrite = wasSending }
        operation()
    }

    private func ownsSentComposition(_ probe: CompositionProbe, sent: SentComposition) -> Bool {
        let anchor = NSRange(location: sent.range.location, length: 0)
        let end = NSRange(location: NSMaxRange(sent.range), length: 0)
        let selectionMatches: Bool
        switch compositionSelectionStyle {
        case .anchor: selectionMatches = probe.selection == anchor
        case .standard: selectionMatches = probe.selection == end || probe.selection == sent.range
        case .unknown: selectionMatches = probe.selection == anchor || probe.selection == end || probe.selection == sent.range
        }
        return isValidRange(sent.range) && probe.markedRange == sent.range &&
            (!client.compositionTextReadbackReliable || (probe.markedText == sent.text && selectionMatches))
    }
}
