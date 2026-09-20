/// IMK client calls can pump the run loop and reenter activation. Only the
/// newest activation may publish a target; older callbacks cannot reclaim it.
final class InputActivation<Controller: AnyObject> {
    private(set) weak var current: Controller?
    private var generation: UInt64 = 0

    func begin(_ controller: Controller) -> UInt64 {
        generation &+= 1
        current = controller
        return generation
    }

    func owns(_ controller: Controller, _ lease: UInt64) -> Bool {
        current === controller && generation == lease
    }

    @discardableResult
    func end(_ controller: Controller, _ lease: UInt64) -> Bool {
        guard owns(controller, lease) else { return false }
        generation &+= 1
        current = nil
        return true
    }
}

/// A controller may be reused for a different client. Only callbacks known to
/// belong to an older sender are discarded; nil/unknown senders remain valid.
final class InputActivationSenders {
    private(set) var current: AnyObject?
    private var retired: [AnyObject] = []
    func begin(_ sender: AnyObject?) {
        if let old = current, let sender, old !== sender {
            retired.append(old)
            if retired.count > 8 { retired.removeFirst() }
        }
        current = sender
        if let sender { retired.removeAll { $0 === sender } }
    }
    func isKnownRetired(_ sender: AnyObject?) -> Bool {
        guard let sender, current !== sender else { return false }
        return retired.contains { $0 === sender }
    }
}
