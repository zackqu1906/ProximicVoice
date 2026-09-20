import Foundation

/// Bounded history for one continuously owned editor activation. No polling,
/// I/O or document reads are added to BEGIN or partial updates.
final class CompositionUndoHistory {
    private(set) var enabled = false
    private var entries: [CompositionUndoRecord] = []
    private var current: CompositionUndoRecord?
    private(set) var retainedUnits = 0
    let capacity: Int
    let unitBudget: Int
    var count: Int { entries.count }
    var totalRetainedUnits: Int { retainedUnits + (current?.retainedUnits ?? 0) }

    init(capacity: Int = 100, unitBudget: Int = 4 * 1024 * 1024) {
        self.capacity = max(0, capacity)
        self.unitBudget = max(0, unitBudget)
    }

    func configure(enabled: Bool) {
        if self.enabled != enabled { clear() }
        self.enabled = enabled
    }

    func clear() {
        entries.removeAll(keepingCapacity: false)
        current = nil
        retainedUnits = 0
    }

    func settled(_ session: CompositionSession, sourceUtteranceID: String? = nil) {
        guard enabled else { return }
        current = session.undoRecord(sourceUtteranceID: sourceUtteranceID)
        // An unrecordable changed operation is a barrier, not a gap that older
        // undo records may jump across. Empty/undone sentences are transparent.
        if current == nil, session.phase != .undone, session.hasModifiedText { clear() }
        trim()
    }

    func begin(_ snapshot: EditorSnapshot) {
        guard enabled else { return }
        if let record = current {
            guard snapshot.selection == record.selection, snapshot.startError == nil else { clear(); return }
            // Reuse the mandatory BEGIN snapshot if it includes this range;
            // do not make another native read or scan earlier history entries.
            let range = record.editedText.map {
                NSRange(location: record.original.readableContext!.range.location,
                        length: ($0 as NSString).length)
            } ?? record.committedRange ?? NSRange(location: record.original.selection.location, length: (record.raw as NSString).length)
            if let text = snapshot.text {
                guard isValidRange(range, length: (text as NSString).length),
                      (text as NSString).substring(with: range) == (record.editedText ?? record.raw) else { clear(); return }
            }
            entries.append(record)
            retainedUnits += record.retainedUnits
            current = nil
            trim()
        } else if let top = entries.last, snapshot.selection != top.selection {
            clear()
        }
    }

    func pop() -> CompositionUndoRecord? {
        guard enabled, let record = entries.popLast() else { return nil }
        retainedUnits -= record.retainedUnits
        current = nil
        return record
    }

    private func trim() {
        // Include the most recent record in the memory budget. Clearing on an
        // oversized edit prevents undoing older text across a missing operation.
        if (current?.retainedUnits ?? 0) > unitBudget { clear(); return }
        while entries.count > capacity || retainedUnits + (current?.retainedUnits ?? 0) > unitBudget {
            guard !entries.isEmpty else { break }
            retainedUnits -= entries.removeFirst().retainedUnits
        }
    }
}
