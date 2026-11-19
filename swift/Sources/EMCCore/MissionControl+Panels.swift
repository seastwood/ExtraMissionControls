import AppKit
import CoreGraphics
import Foundation

extension NSWindow.Level {
    static func + (level: NSWindow.Level, offset: Int) -> NSWindow.Level {
        NSWindow.Level(rawValue: level.rawValue + offset)
    }

    static func - (level: NSWindow.Level, offset: Int) -> NSWindow.Level {
        NSWindow.Level(rawValue: level.rawValue - offset)
    }
}

/// Stable slot assignment shared by the button, scrim and tray pools: each
/// key keeps the SAME pooled panel from frame to frame (exact key first, else
/// a lone same-identity panel from last frame — the tile moved), and a new key
/// takes a panel that was NOT on screen last frame, so nothing visibly slides
/// from one window to another.
func assignSlots<Key: Hashable>(_ keys: [Key], previous: [Key: Int],
                                sameIdentity: (Key, Key) -> Bool) -> [Int] {
    var assigned = [Int?](repeating: nil, count: keys.count)
    var used = Set<Int>()
    for (i, key) in keys.enumerated() {
        if let index = previous[key], !used.contains(index) {
            assigned[i] = index
            used.insert(index)
        }
    }
    for (i, key) in keys.enumerated() where assigned[i] == nil {
        let same = previous.filter { sameIdentity($0.key, key) && !used.contains($0.value) }.map { $0.value }
        if same.count == 1 {
            assigned[i] = same[0]
            used.insert(same[0])
        }
    }
    var busy = used.union(previous.values)
    var free = 0
    for i in keys.indices where assigned[i] == nil {
        while busy.contains(free) { free += 1 }
        assigned[i] = free
        used.insert(free)
        busy.insert(free)
    }
    return assigned.map { $0! }
}

extension MissionControlController {
    var popUpLevel: NSWindow.Level { .popUpMenu }

    // MARK: - button panels

    func syncPanels(_ targetList: [Target]) {
        var trays: [TileKey: [Double]] = [:]
        var trayOrder: [TileKey] = []
        let keys = targetList.map { t in
            PanelKey(kind: t.kind, title: t.tile.title, slot: t.tile.slot,
                     x: Int(t.tile.x.rounded()), y: Int(t.tile.y.rounded()))
        }
        let assigned = assignSlots(keys, previous: panelKeys) {
            $0.kind == $1.kind && $0.title == $1.title && $0.slot == $1.slot
        }
        panelKeys = Dictionary(zip(keys, assigned), uniquingKeysWith: { a, _ in a })
        let used = Set(assigned)
        for (index, target) in zip(assigned, targetList) {
            let tile = target.tile
            let panel = panelAt(index)
            let button = buttons[index]
            // Scale down on small tiles (collapsed Spaces Bar).
            let size = min(Double(UI.closeButtonSize), max(Tuning.minButtonSize, tile.height * 0.45))
            let x = tile.x + Tuning.buttonInset + Double(tile.slot) * (size + Tuning.buttonGap)
            let yTop = tile.y + Tuning.buttonInset
            let rect = Rect(x, yTop, size, size)
            // In a settled overview the layout is identical frame to frame:
            // skip the window move (it forces a redraw).
            let moved = rects[index] != rect || fadeTarget[ObjectIdentifier(panel)] != 1
            rects[index] = rect
            targets[index] = ButtonTarget(kind: target.kind, title: tile.title, element: tile.element)
            // Grow this tile's tray box to enclose the button.
            let trayKey = TileKey(title: tile.title, x: Int(tile.x.rounded()), y: Int(tile.y.rounded()))
            if var box = trays[trayKey] {
                box[0] = min(box[0], x); box[1] = min(box[1], yTop)
                box[2] = max(box[2], x + size); box[3] = max(box[3], yTop + size)
                trays[trayKey] = box
            } else {
                trays[trayKey] = [x, yTop, x + size, yTop + size]
                trayOrder.append(trayKey)
            }
            if moved {
                let y = (screenHeight ?? 0) - yTop - size  // flip to bottom-left
                panel.setFrame(NSRect(x: x, y: y, width: size, height: size), display: true)
            }
            // Style sends only when something about THIS button changed.
            let label = tile.label ?? "✕"
            let hover = tile.hover ?? UI.hoverRed
            let applied = buttonApplied[index]
            if applied == nil || applied!.label != label || applied!.hover != hover || applied!.size != size {
                button.setLabel(label)
                button.setHoverRGB(hover)
                if applied == nil || abs(applied!.size - size) > 0.5 { button.setDiameter(CGFloat(size)) }
                buttonApplied[index] = (label, hover, size)
            }
            if moved {
                Log.debug(String(format: "panel %d (%@ '%@') shown at (%.0f,%.0f) size=%.0f",
                                 index, target.kind, tile.title, x, yTop, size))
            }
            showPanel(panel)
        }
        for index in panels.indices where !used.contains(index) {
            hidePanel(panels[index])
            targets[index] = nil
            rects[index] = nil
        }
        if Log.debugEnabled, !keys.isEmpty, keys != loggedPanelKeys {
            loggedPanelKeys = keys
            let sample = zip(assigned, keys).prefix(6).map { index, key -> String in
                let p = panels[index]
                return String(format: "%d:%@/%@ vis=%d a=%.2f @(%.0f,%.0f %.0fx%.0f)", index, key.kind,
                              String(key.title.prefix(18)), p.isVisible ? 1 : 0, p.alphaValue,
                              p.frame.origin.x, p.frame.origin.y, p.frame.width, p.frame.height)
            }
            Log.geom("PANELS n=\(keys.count) pool=\(panels.count) \(sample.joined(separator: " | "))")
        }
        syncTrays(trays, order: trayOrder)
    }

    func panelAt(_ index: Int) -> NSPanel {
        while panels.count <= index { panels.append(makeButtonPanel(panels.count)) }
        return panels[index]
    }

    /// A borderless, non-activating overlay panel above Mission Control's
    /// backdrop, on every space, pinned in place while MC animates.
    func makeOverlayPanel(_ frame: NSRect, level: NSWindow.Level, ignoresMouse: Bool = true) -> NSPanel {
        let panel = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isReleasedWhenClosed = false
        panel.sharingType = Tuning.panelSharing
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        // Click-through: clicks reach us through the event tap and hover is
        // polled, so macOS still sees the pointer over the tile underneath.
        panel.ignoresMouseEvents = ignoresMouse
        panel.level = level
        // All spaces: active-space-only panels made Mission Control re-lay out
        // the whole overview on each click (macOS 27).
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        return panel
    }

    func makeButtonPanel(_ index: Int) -> NSPanel {
        let size = UI.closeButtonSize
        let panel = makeOverlayPanel(NSRect(x: 0, y: 0, width: size, height: size), level: popUpLevel)
        let button = UI.makeCloseButton(target: self, action: #selector(closeClicked(_:)))
        button.tag = index
        panel.contentView?.addSubview(button.outerView)
        buttons.append(button)
        return panel
    }

    // MARK: - "marked" dim overlays

    func syncMarks(_ marks: [Mark]) {
        let keys = marks.map { TileKey(title: $0.tile.title, x: Int($0.tile.x.rounded()), y: Int($0.tile.y.rounded())) }
        let assigned = assignSlots(keys, previous: markKeys) { $0.title == $1.title }
        markKeys = Dictionary(zip(keys, assigned), uniquingKeysWith: { a, _ in a })
        let used = Set(assigned)
        for (index, mark) in zip(assigned, marks) {
            let panel = markPanelAt(index)
            let tile = mark.tile
            let w = tile.width, h = tile.height
            let state = [round1(tile.x), round1(tile.y), round1(w), round1(h)]
            let view = panel.contentView as? MarkView
            // Settled frames repeat identically: only touch the window when
            // the scrim actually moved/changed.
            if markApplied[index] != state || !panel.isVisible {
                markApplied[index] = state
                let y = (screenHeight ?? 0) - tile.y - h
                panel.setFrame(NSRect(x: tile.x, y: y, width: w, height: h), display: true)
                if let view = view, abs(Double(view.frame.width) - w) > 0.5 || abs(Double(view.frame.height) - h) > 0.5 {
                    view.frame = NSRect(x: 0, y: 0, width: w, height: h)
                    view.needsDisplay = true
                }
            }
            view?.setGlyph(mark.glyph)
            showPanel(panel)
        }
        for index in markPanels.indices where !used.contains(index) {
            hidePanel(markPanels[index])
            markApplied[index] = nil
        }
    }

    private func round1(_ v: Double) -> Double { (v * 10).rounded() / 10 }

    func markPanelAt(_ index: Int) -> NSPanel {
        while markPanels.count <= index {
            // Just under the button panels so a button is never covered.
            let panel = makeOverlayPanel(NSRect(x: 0, y: 0, width: 10, height: 10), level: popUpLevel - 1)
            panel.contentView = UI.makeMarkOverlay(NSRect(x: 0, y: 0, width: 10, height: 10))
            markPanels.append(panel)
        }
        return markPanels[index]
    }

    // MARK: - trays behind each tile's button row

    func syncTrays(_ boxes: [TileKey: [Double]], order: [TileKey]) {
        let pad = 3.0
        let assigned = assignSlots(order, previous: trayKeys) { $0.title == $1.title }
        trayKeys = Dictionary(zip(order, assigned), uniquingKeysWith: { a, _ in a })
        let used = Set(assigned)
        for (i, key) in zip(assigned, order) {
            guard let box = boxes[key] else { continue }
            let tx = box[0] - pad, ty = box[1] - pad
            let tw = box[2] - box[0] + 2 * pad, th = box[3] - box[1] + 2 * pad
            let panel = trayPanelAt(i)
            let state = [round1(tx), round1(ty), round1(tw), round1(th)]
            // Glass redraws are the most expensive part of a frame.
            if trayApplied[i] != state || !panel.isVisible {
                trayApplied[i] = state
                let y = (screenHeight ?? 0) - ty - th
                panel.setFrame(NSRect(x: tx, y: y, width: tw, height: th), display: true)
            }
            showPanel(panel)
        }
        for i in trayPanels.indices where !used.contains(i) {
            hidePanel(trayPanels[i])
            trayApplied[i] = nil
        }
    }

    func trayPanelAt(_ index: Int) -> NSPanel {
        while trayPanels.count <= index {
            let size = UI.closeButtonSize
            let frame = NSRect(x: 0, y: 0, width: size, height: size)
            let panel = makeOverlayPanel(frame, level: popUpLevel - 1)
            let tray = UI.makeTray(frame, cornerRadius: 9)
            tray.autoresizingMask = [.width, .height]
            panel.contentView = tray
            trayPanels.append(panel)
        }
        return trayPanels[index]
    }

    // MARK: - fades

    /// Fade a panel in. Idempotent once shown (every order-out resets its fade
    /// target, so the target alone is trustworthy).
    func showPanel(_ panel: NSPanel) {
        let id = ObjectIdentifier(panel)
        if fadeTarget[id] == 1 { return }
        fadeTarget[id] = 1
        if !panel.isVisible {
            panel.alphaValue = 0
            panel.orderFrontRegardless()
        }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = Tuning.fadeIn
            panel.animator().alphaValue = 1
        }
    }

    /// Fade a panel out, then order it out — unless a later showPanel
    /// cancels the pending fade.
    func hidePanel(_ panel: NSPanel) {
        let id = ObjectIdentifier(panel)
        if (fadeTarget[id] ?? 0) == 0 { return }
        fadeTarget[id] = 0
        if !panel.isVisible {
            panel.orderOut(nil)
            return
        }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = Tuning.fadeOut
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self, weak panel] in
            guard let self = self, let panel = panel else { return }
            if (self.fadeTarget[ObjectIdentifier(panel)] ?? 0) == 0 { panel.orderOut(nil) }
        })
    }

    /// Order a pooled button out NOW (an acted-on button must vanish at once).
    func dropButton(_ index: Int) {
        guard index < panels.count else { return }
        fadeTarget[ObjectIdentifier(panels[index])] = 0
        panels[index].orderOut(nil)
        rects[index] = nil
    }

    // MARK: - scene probe

    /// Compare a window's CG-reported bounds to the frame we set: true/false,
    /// or nil when it has no on-screen entry. 5px absorbs the WindowServer's
    /// small constant offset; the zoom moves panels by tens of pixels.
    func boundsMatch(_ number: Int, _ expect: Rect) -> Bool? {
        guard number > 0 else { return nil }
        guard let info = WindowList.query(.optionIncludingWindow, CGWindowID(number)).first else { return nil }
        guard let b = info.bounds else { return nil }
        return abs(Double(b.origin.x) - expect.x) <= 5 && abs(Double(b.origin.y) - expect.y) <= 5
            && abs(Double(b.width) - expect.w) <= 5 && abs(Double(b.height) - expect.h) <= 5
    }

    /// Raise the invisible scene probe: a SCREEN-SIZED alpha-0 panel. The
    /// WindowServer applies Mission Control's zoom to our windows, so while
    /// the scene is even slightly off rest its CG bounds diverge from this.
    func showSceneProbe() {
        let w = screenWidth ?? 1000, h = screenHeight ?? 800
        if Tuning.splitSceneProbe {
            showCornerProbes(w, h)
            return
        }
        if sceneProbe == nil {
            let panel = makeOverlayPanel(NSRect(x: 0, y: 0, width: w, height: h), level: popUpLevel)
            panel.alphaValue = 0
            sceneProbe = panel
        }
        sceneProbeRect = Rect(0, 0, w, h)
        sceneProbe?.setFrame(NSRect(x: 0, y: 0, width: w, height: h), display: false)
        sceneProbe?.orderFrontRegardless()
    }

    /// macOS 27 variant: WindowManager draws every window into the Spaces Bar
    /// previews IGNORING its alpha, so the screen-sized probe painted each
    /// preview black. Two tiny probes at opposite corners see the same zoom.
    func showCornerProbes(_ w: Double, _ h: Double) {
        let size = Tuning.cornerProbeSize
        // Cocoa (bottom-left) rects; CG/top-left expectations beside.
        let specs: [(NSRect, Rect)] = [
            (NSRect(x: 0, y: h - size, width: size, height: size), Rect(0, 0, size, size)),
            (NSRect(x: w - size, y: 0, width: size, height: size), Rect(w - size, h - size, size, size)),
        ]
        if cornerProbes.isEmpty {
            cornerProbes = specs.map { spec in
                let panel = makeOverlayPanel(spec.0, level: popUpLevel)
                panel.alphaValue = 0
                return panel
            }
            sceneProbe = cornerProbes.first
        }
        cornerRects = []
        for (panel, spec) in zip(cornerProbes, specs) {
            panel.setFrame(spec.0, display: false)
            panel.orderFrontRegardless()
            cornerRects.append((panel, spec.1))
        }
        sceneProbeRect = cornerRects.first?.expect
    }

    /// true (at rest) / false (transformed) / nil (unreadable).
    func probeMatch() -> Bool? {
        if !Tuning.splitSceneProbe {
            guard let probe = sceneProbe, let rect = sceneProbeRect else { return nil }
            return boundsMatch(probe.windowNumber, rect)
        }
        let results = cornerRects.map { boundsMatch($0.panel.windowNumber, $0.expect) }
        if results.isEmpty || results.contains(where: { $0 == nil }) { return nil }
        return results.allSatisfy { $0 == true }
    }

    /// 30 Hz while Mission Control is up: keep sceneStreak = consecutive
    /// at-rest reads. Buttons only exist when the scene is stable; the moment
    /// a transform starts under visible buttons they're hidden.
    func checkSceneTransform() {
        guard active, sceneProbe != nil, sceneProbeRect != nil else { return }
        // Settled: 10Hz is plenty to spot a NEW transform.
        probeDecim = (probeDecim + 1) % 3
        if settledThumbsSig != nil && probeDecim != 0 { return }
        let match = probeMatch()
        if match == true {
            sceneStreak += 1
            sceneDeadTicks = 0
            sceneClosedTicks = 0
            return
        }
        let wasOpen = sceneStreak >= Tuning.sceneStreakMin
        sceneStreak = 0
        if match == false && wasOpen && !rects.isEmpty {
            Log.geom("SCENE-TRANSFORM under visible buttons: hide")
            hideAll(immediate: true)
        }
        // Tiered fail-safes: only an UNREADABLE probe means the signal broke.
        if match == nil { sceneDeadTicks += 1 } else { sceneDeadTicks = 0 }
        sceneClosedTicks += 1
        if sceneDeadTicks >= Tuning.sceneDeadTicksMax || sceneClosedTicks >= Tuning.sceneClosedTicksMax {
            Log.geom("SCENE-PROBE fail-safe: forcing gate open (dead=\(sceneDeadTicks) closed=\(sceneClosedTicks))")
            sceneStreak = Tuning.sceneStreakMin
            sceneDeadTicks = 0
            sceneClosedTicks = 0
        }
    }

    /// A probable exit (a click passing through, the active space changing):
    /// hide everything now and keep it hidden while the layout stays frozen,
    /// up to Tuning.exitLatchMax.
    func latchExit() {
        exitLatched = true
        exitLatchSig = prevLayout
        exitLatchExpires = Clock.now() + Tuning.exitLatchMax
        hideAll(immediate: true)
    }

    /// Drop every pooled panel so the next session recreates them under the
    /// current style settings (read at view creation).
    func rebuildStyle() {
        hideAll(immediate: true)
        (panels + markPanels + trayPanels).forEach { $0.close() }
        panels = []
        buttons = []
        panelKeys = [:]
        trayKeys = [:]
        markKeys = [:]
        markPanels = []
        trayPanels = []
        fadeTarget = [:]
        buttonApplied = [:]
        hoverIndex = nil
        menuPanel?.close()
        menuPanel = nil
        menuView = nil
    }

    /// Hide every overlay; `immediate` skips the fade.
    func hideAll(immediate: Bool = false) {
        closeMenu()
        for panel in panels + markPanels + trayPanels {
            if immediate {
                fadeTarget[ObjectIdentifier(panel)] = 0  // cancel any pending fade-in
                panel.orderOut(nil)
            } else {
                hidePanel(panel)
            }
        }
        targets = [:]
        rects = [:]
        markApplied = [:]
        trayApplied = [:]
    }
}
