import Foundation

protocol HandoffClient: CompositionClient {
    /// Update stale marking with an empty preedit at an explicit zero range.
    /// The caller must establish document-access support and a collapsed caret.
    func endStaleComposition(at caret: NSRange)
}

/// A bounded activation handoff. No ASR transaction may write through this client
/// until the handoff is ready. A keyboard event or deactivation cancels it.
final class CompositionHandoff {
    enum State: String { case pending, ready, failed }
    private(set) var state: State = .pending
    private(set) var error = ""
    private(set) var diagnostics: [String: Any] = [
        "action": "waiting", "write_action": "", "writes": 0, "selection": [-1, -1],
        "marked_range": [-1, -1], "marked_characters": -1,
        "document_access": false, "text_queried": false,
    ]

    private let client: HandoffClient
    private let now: () -> TimeInterval
    private let deadline: TimeInterval
    private var documentAccess: Bool?
    private var previousProbe: CompositionProbe?
    private var previousProbeTime: TimeInterval?
    private var stableProbes = 0
    private var writes = 0
    private var advancing = false

    init(client: HandoffClient,
         now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         timeout: TimeInterval = 0.5) {
        self.client = client
        self.now = now
        deadline = now() + max(0, timeout)
    }

    func advance() {
        guard state == .pending, !advancing else { return }
        advancing = true
        defer { advancing = false }
        if documentAccess == nil {
            documentAccess = (try? client.snapshot().documentAccess) ?? false
        }
        guard state == .pending else { return }
        let time = now()
        let probe = client.probe()
        guard state == .pending else { return }
        let validSelection = isValidRange(probe.selection)
        let hasMarkedText = isValidRange(probe.markedRange) && probe.markedRange.length > 0
        // NativeInputClient.probe performs the range query for every valid mark
        // up to this shared transport limit, even when document length is zero.
        let textQueried = hasMarkedText && probe.markedRange.length <= 512 * 1024
        diagnostics["selection"] = numbers(probe.selection)
        diagnostics["marked_range"] = numbers(probe.markedRange)
        diagnostics["marked_characters"] = probe.markedText.map { ($0 as NSString).length } ?? -1
        diagnostics["document_access"] = documentAccess == true
        diagnostics["text_queried"] = textQueried

        if validSelection && !hasMarkedText {
            state = .ready
            diagnostics["action"] = "ready"
            return
        }
        guard now() < deadline else {
            state = .failed
            diagnostics["action"] = "failed"
            error = "输入法交接未完成，当前输入框暂未提供一致的输入状态"
            return
        }
        // A sent handoff write may acknowledge asynchronously. Never replay it, or
        // replace it with a more destructive fallback when readback is delayed.
        guard writes == 0 else { return }

        if let previousProbe, same(previousProbe, probe),
           let previousProbeTime, time > previousProbeTime {
            stableProbes += 1
        } else if previousProbeTime == nil || previousProbeTime != time ||
                    previousProbe.map({ !same($0, probe) }) == true {
            stableProbes = 1
        }
        previousProbe = probe
        previousProbeTime = time
        guard stableProbes >= 2, validSelection, hasMarkedText, textQueried else { return }

        let marked = probe.markedRange
        let selection = probe.selection
        let selectionInsideMark = selection.location >= marked.location &&
            NSMaxRange(selection) <= NSMaxRange(marked)
        if selectionInsideMark, documentAccess == true, let text = probe.markedText,
           (text as NSString).length == marked.length {
            recordWrite("commit_previous")
            // Replace precisely the range that was read. If the old input
            // method already committed and only its range cache remains, a
            // default insertion could otherwise duplicate that committed text.
            client.replace(marked, with: text)
        } else if selection.length == 0,
                  (selection.location < marked.location || selection.location > NSMaxRange(marked)),
                  probe.markedText == nil, documentAccess == true {
            recordWrite("reset_stale")
            // Repeated, unreadable marking detached from the caret is our
            // activation-only stale-cache candidate, not an IMK guarantee.
            // Use one empty marked-text update at an explicit zero range;
            // unspecified replacement can delete real text. This is not a
            // general way to preserve live preedit across all client toolkits.
            // The next ticks verify without retrying or adding an empty insert.
            client.endStaleComposition(at: selection)
        }
    }

    func cancel() {
        guard state == .pending else { return }
        state = .failed
        error = ""
        diagnostics["action"] = "cancelled"
    }

    private func recordWrite(_ action: String) {
        writes += 1
        diagnostics["writes"] = writes
        diagnostics["action"] = action
        diagnostics["write_action"] = action
    }

    private func numbers(_ range: NSRange) -> [Int] {
        isValidRange(range) ? [range.location, range.length] : [-1, -1]
    }

    private func same(_ lhs: CompositionProbe, _ rhs: CompositionProbe) -> Bool {
        lhs.selection == rhs.selection && lhs.markedRange == rhs.markedRange &&
            lhs.markedText == rhs.markedText
    }
}
