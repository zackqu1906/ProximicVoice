import Foundation

/// Called only after the transaction has validated the exact committed range.
/// Committed content is replaced with insertText, including empty replacements.
/// Live marked-text cancellation has a separate delivery path.
enum CommittedReplacementDelivery {
    static func send(_ text: String, range: NSRange,
                     insert: (String, NSRange) -> Void) {
        guard isValidRange(range), !text.isEmpty || range.length > 0 else { return }
        insert(text, range)
    }
}
