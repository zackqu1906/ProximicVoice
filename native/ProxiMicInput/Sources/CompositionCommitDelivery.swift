import Foundation

/// Delivers a commit after CompositionSession has verified ownership of its
/// composition. This must not be used for committed document replacements.
enum CompositionCommitDelivery {
    static func send(_ text: String, markedRange: () -> NSRange,
                     clearMarkedText: () -> Void, insertText: (String) -> Void) {
        if text.isEmpty {
            let marked = markedRange()
            if isValidRange(marked), marked.length > 0 {
                // A zero-length insert is not reliably delivered by every input
                // client. Explicitly clear our preedit using the public marked
                // text API first; the empty insert then ends the composition.
                // Recheck that a mark still exists so this extra write cannot
                // create a new composition after the client has ended it.
                clearMarkedText()
            }
        }
        insertText(text)
    }
}
