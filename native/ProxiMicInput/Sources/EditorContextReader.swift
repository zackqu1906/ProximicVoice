import Foundation

/// Reads an explicitly bounded context window. A client may reject an
/// out-of-bounds request instead of clipping it, even when in-bounds reads work.
/// An unknown document length never becomes a claim of complete context.
enum EditorContextReader {
    static func read(around selection: NSRange,
                     query: (NSRange) -> EditorTextRange?) -> EditorTextRange? {
        let budget = 32 * 1024
        guard isValidRange(selection), selection.length <= 64 * 1024,
              NSMaxRange(selection) <= Int.max - budget else { return nil }
        func attempt(_ range: NSRange) -> EditorTextRange? {
            guard let result = query(range), isValidRange(result.range),
                  (result.text as NSString).length == result.range.length,
                  result.range.location >= range.location, NSMaxRange(result.range) <= NSMaxRange(range),
                  result.range.location <= selection.location,
                  NSMaxRange(result.range) >= NSMaxRange(selection) else { return nil }
            return result
        }
        var before = min(selection.location, budget)
        let wide = NSRange(location: selection.location - before, length: before + selection.length + budget)
        if let clipped = attempt(wide) { return clipped }

        // The caret/selection gives a known document boundary. Start with text
        // ending there rather than repeatedly asking beyond the document end.
        var best: EditorTextRange?
        repeat {
            let anchored = NSRange(location: selection.location - before, length: before + selection.length)
            best = attempt(anchored)
            if best != nil || before == 0 { break }
            before /= 2
        } while true

        // Also include readable text after the selection, including a caret at
        // the start of the field. Failed probes are not proof of document EOF.
        let start = best?.range.location ?? selection.location
        let baseEnd = NSMaxRange(selection)
        var low = 0
        var high = 1
        while high <= budget {
            let proposed = NSRange(location: start, length: baseEnd - start + high)
            if let result = attempt(proposed) {
                best = result
                if NSMaxRange(result.range) < NSMaxRange(proposed) { return best }
                low = high
                if high == budget { return best }
                high = min(budget, high * 2)
            } else { break }
        }
        while high - low > 1 {
            let middle = low + (high - low) / 2
            let proposed = NSRange(location: start, length: baseEnd - start + middle)
            if let result = attempt(proposed) {
                best = result
                if NSMaxRange(result.range) < NSMaxRange(proposed) { return best }
                low = middle
            } else { high = middle }
        }
        return best
    }
}
