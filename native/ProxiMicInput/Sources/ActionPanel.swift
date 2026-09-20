import AppKit

private final class PassivePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Native IME palette. It never activates the input method or steals the client caret.
final class ActionPanel: NSObject {
    private let panel: NSPanel
    private let effect = NSVisualEffectView()
    private let errorTint = NSView()
    private let status = NSTextField(labelWithString: "听写中")
    private let progress = NSProgressIndicator()
    private let cancelButton = NSButton(title: "撤销", target: nil, action: nil)
    private let convertButton = NSButton(title: "转换为编辑", target: nil, action: nil)
    private let stack: NSStackView
    private var dismissTimer: Timer?
    private var shouldShow = false
    private var anchor = ActionPanelAnchor()
    private var busy = false
    var needsPosition: Bool { shouldShow && (anchor.needsCaret || !panel.isVisible) }
    var positioningDiagnostics: [String: Any] {
        ["shown": shouldShow, "visible": panel.isVisible, "application_hidden": NSApp.isHidden, "needs_position": needsPosition,
         "frozen": anchor.heldOrigin != nil, "frame": CaretDiagnostics.rect(panel.frame),
         "anchor_caret": anchor.caret.map(CaretDiagnostics.rect) ?? []]
    }
    var onCancel: (() -> Void)?
    var onConvert: (() -> Void)?

    override init() {
        panel = PassivePanel(contentRect: NSRect(x: 0, y: 0, width: 290, height: 42),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        stack = NSStackView(views: [progress, status, cancelButton, convertButton])
        super.init()
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.animationBehavior = .none
        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 10
        effect.layer?.masksToBounds = true
        panel.contentView = effect
        errorTint.identifier = NSUserInterfaceItemIdentifier("errorTint")
        errorTint.wantsLayer = true
        errorTint.layer?.backgroundColor = NSColor(srgbRed: 0.43, green: 0.06, blue: 0.09, alpha: 0.96).cgColor
        errorTint.isHidden = true
        errorTint.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(errorTint)
        NSLayoutConstraint.activate([
            errorTint.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
            errorTint.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
            errorTint.topAnchor.constraint(equalTo: effect.topAnchor),
            errorTint.bottomAnchor.constraint(equalTo: effect.bottomAnchor)
        ])
        status.identifier = NSUserInterfaceItemIdentifier("status")
        status.font = .systemFont(ofSize: 12, weight: .medium)
        status.textColor = .labelColor
        progress.style = .spinning
        progress.controlSize = .small
        progress.isIndeterminate = true
        progress.isDisplayedWhenStopped = false
        progress.isHidden = true
        progress.setAccessibilityLabel("正在处理")
        progress.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            progress.widthAnchor.constraint(equalToConstant: 16),
            progress.heightAnchor.constraint(equalToConstant: 16)
        ])
        status.setContentCompressionResistancePriority(.required, for: .horizontal)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 11),
            stack.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -9),
            stack.centerYAnchor.constraint(equalTo: effect.centerYAnchor)
        ])
        for button in [cancelButton, convertButton] {
            button.bezelStyle = .roundRect
            button.controlSize = .small
            button.font = .systemFont(ofSize: 12)
            button.focusRingType = .none
            button.target = self
        }
        cancelButton.action = #selector(cancel)
        convertButton.action = #selector(convert)
    }

    @objc private func cancel() { onCancel?() }
    @objc private func convert() { onConvert?() }

    func applyPresentation(_ presentation: ActionPanelPresentation) {
        status.stringValue = presentation.title
        status.toolTip = presentation.detail
        status.textColor = presentation.isError ? .white : .labelColor
        errorTint.isHidden = !presentation.isError
        effect.appearance = presentation.isError ? NSAppearance(named: .darkAqua) : nil
        effect.layer?.borderWidth = presentation.isError ? 1 : 0
        effect.layer?.borderColor = presentation.isError ? NSColor.systemRed.cgColor : nil
    }

    func update(_ session: CompositionSession, historyDepth: Int = 0) {
        anchor.update(utteranceID: session.utteranceID,
                      editing: session.phase == .editing ||
                          (session.phase == .finishing && session.editRequested) ||
                          (session.phase == .edited && session.awaitingReadback),
                      currentSize: panel.frame.size)
        dismissTimer?.invalidate()
        dismissTimer = nil
        let presentation = ActionPanelPresentation(phase: session.phase, error: session.error,
            empty: session.raw.isEmpty, editRequested: session.editRequested,
            readableRange: session.editScope == "readable_range")
        applyPresentation(presentation)
        let nextBusy = !presentation.isError && (session.phase == .editing || session.phase == .finishing)
        if nextBusy != busy {
            busy = nextBusy
            progress.isHidden = !busy
            if busy { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
        }
        cancelButton.title = session.cancelLabel
        cancelButton.isHidden = !session.canCancel && !(session.phase == .undone && historyDepth > 0)
        convertButton.isHidden = !session.canEdit
        shouldShow = true
        stack.layoutSubtreeIfNeeded()
        let size = anchor.layoutSize(NSSize(width: max(132, stack.fittingSize.width + 20), height: 40), busy: busy)
        if panel.frame.size != size { panel.setContentSize(size) }
        if let origin = anchor.heldOrigin, panel.frame.origin != origin { panel.setFrameOrigin(origin) }
        if [.dictated, .edited, .undone, .interrupted, .error].contains(session.phase) {
            dismissTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in self?.hide() }
        }
    }

    func position(caret: NSRect?, clientLevel: Int32) {
        guard shouldShow else { return }
        // Some clients temporarily stop returning geometry while waiting for
        // a result. Keep this utterance's anchor even across temporary hides.
        guard let caret = anchor.observe(caret), let screen = NSScreen.screens.first(where: { $0.frame.intersects(caret.insetBy(dx: -1, dy: -1)) }) else {
            panel.orderOut(nil)
            return
        }
        let visible = screen.visibleFrame.insetBy(dx: 6, dy: 6)
        let size = panel.frame.size
        let x = min(max(caret.minX, visible.minX), max(visible.minX, visible.maxX - size.width))
        var y = caret.minY - size.height - 6
        if y < visible.minY { y = caret.maxY + 6 }
        y = min(max(y, visible.minY), max(visible.minY, visible.maxY - size.height))
        let scale = screen.backingScaleFactor
        let origin = anchor.place(NSPoint(x: (x * scale).rounded() / scale, y: (y * scale).rounded() / scale), size: size)
        if panel.frame.origin != origin { panel.setFrameOrigin(origin) }
        panel.level = NSWindow.Level(rawValue: max(NSWindow.Level.floating.rawValue, Int(clientLevel) + 1))
        if !panel.isVisible {
            // A hidden accessory process cannot show even orderFrontRegardless.
            // Unhide without activation so the editor keeps keyboard focus.
            if NSApp.isHidden { NSApp.unhideWithoutActivation() }
            panel.orderFrontRegardless()
        }
    }

    func hide() {
        shouldShow = false
        // IMK may hide palettes while ending a composition or preparing an
        // edit. Its next state update must reuse this operation's anchor.
        // ActionPanelAnchor.update resets it when a new utterance starts.
        busy = false
        progress.stopAnimation(nil)
        progress.isHidden = true
        dismissTimer?.invalidate()
        dismissTimer = nil
        panel.orderOut(nil)
    }

    deinit { dismissTimer?.invalidate() }
}
