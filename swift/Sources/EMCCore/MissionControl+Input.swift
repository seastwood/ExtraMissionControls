import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

/// C callbacks for the event taps; `refcon` is the controller.
private func emcMouseTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                                 refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let refcon = refcon else { return Unmanaged.passUnretained(event) }
    let controller = Unmanaged<MissionControlController>.fromOpaque(refcon).takeUnretainedValue()
    return controller.handleMouseTap(type, event) ? Unmanaged.passUnretained(event) : nil
}

private func emcKeyTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                               refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let refcon = refcon else { return Unmanaged.passUnretained(event) }
    let controller = Unmanaged<MissionControlController>.fromOpaque(refcon).takeUnretainedValue()
    return controller.handleKeyTap(type, event) ? Unmanaged.passUnretained(event) : nil
}

private func maskBit(_ type: CGEventType) -> CGEventMask { CGEventMask(1) << CGEventMask(type.rawValue) }

extension MissionControlController {
    // MARK: - click interception

    func setTapEnabled(_ enabled: Bool) {
        if tap == nil {
            if !enabled { return }
            let refcon = Unmanaged.passUnretained(self).toOpaque()
            // Middle-click a thumbnail to close it (this tap is only enabled
            // while Mission Control is up).
            let mouseMask = maskBit(.leftMouseDown) | maskBit(.leftMouseUp) | maskBit(.rightMouseDown)
                | maskBit(.rightMouseUp) | maskBit(.otherMouseDown) | maskBit(.otherMouseUp)
            // Also listen (never consume) for raw trackpad touch frames: "no
            // touch frames lately" = fingers lifted = no gesture mid-flight.
            tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                    eventsOfInterest: mouseMask | (CGEventMask(1) << CGEventMask(Tuning.touchEventType)),
                                    callback: emcMouseTapCallback, userInfo: refcon)
            if tap == nil {
                // Some configuration refused the gesture type: mouse only.
                tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                        eventsOfInterest: mouseMask, callback: emcMouseTapCallback, userInfo: refcon)
            }
            guard let tap = tap else {
                Log.note("could not create event tap — buttons in Mission Control will not receive clicks "
                         + "(is Accessibility permission granted?)")
                return
            }
            tapSource = CFMachPortCreateRunLoopSource(nil, tap, 0)
            CFRunLoopAddSource(CFRunLoopGetMain(), tapSource, .commonModes)
        }
        if let tap = tap { CGEvent.tapEnable(tap: tap, enable: enabled) }
        setKeyTapEnabled(enabled)
        if !enabled {
            swallowMouseUp = false
            swallowRightUp = false
            closeMenu()
        }
    }

    /// A SEPARATE tap for the W / M / Q shortcuts: keyboard taps can need
    /// Input Monitoring permission, and if macOS refuses this one only the
    /// shortcuts are lost.
    func setKeyTapEnabled(_ enabled: Bool) {
        if keyTap == nil {
            if !enabled || keyTapFailed { return }
            keyTap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                       eventsOfInterest: maskBit(.keyDown), callback: emcKeyTapCallback,
                                       userInfo: Unmanaged.passUnretained(self).toOpaque())
            guard let keyTap = keyTap else {
                keyTapFailed = true
                Log.note("keyboard shortcuts (W/M/Q) are unavailable — macOS refused the keyboard event tap "
                         + "(Input Monitoring permission).")
                return
            }
            keyTapSource = CFMachPortCreateRunLoopSource(nil, keyTap, 0)
            CFRunLoopAddSource(CFRunLoopGetMain(), keyTapSource, .commonModes)
        }
        if let keyTap = keyTap { CGEvent.tapEnable(tap: keyTap, enable: enabled) }
    }

    /// Returns true to pass the event on, false to consume it.
    func handleKeyTap(_ type: CGEventType, _ event: CGEvent) -> Bool {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if active, let keyTap = keyTap { CGEvent.tapEnable(tap: keyTap, enable: true) }
            return true
        }
        guard type == .keyDown else { return true }
        let location = event.location
        return tapKey(event, Double(location.x), Double(location.y))
    }

    /// Returns true to pass the event on, false to consume it.
    func handleMouseTap(_ type: CGEventType, _ event: CGEvent) -> Bool {
        if type.rawValue == Tuning.touchEventType {
            lastTouch = Clock.now()  // fingers on the trackpad right now
            return true
        }
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if active, let tap = tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return true
        }
        if event.getIntegerValueField(.eventSourceUserData) == Tuning.syntheticMouseTag {
            return true  // our own posted mouse input
        }
        let x = Double(event.location.x), y = Double(event.location.y)
        switch type {
        case .otherMouseDown:
            let button = event.getIntegerValueField(.mouseEventButtonNumber)
            if button == 2 && !menuOpen, let thumb = thumbAt(x, y) {
                swallowOtherUp = true
                let title = thumb.title
                DispatchQueue.main.async { self.shortcutAction("close", title) }
                return false
            }
            return true
        case .otherMouseUp:
            if swallowOtherUp {
                swallowOtherUp = false
                return false
            }
            return true
        case .leftMouseDown:
            if menuOpen {
                // Pick a row, or dismiss. The Dock bubble opens on hover, so a
                // click outside it dismisses it AND goes through.
                if let row = menuRowAt(x, y) {
                    swallowMouseUp = true
                    DispatchQueue.main.async { self.menuPick(row) }
                    return false
                }
                if menuKind != "dock" {
                    swallowMouseUp = true
                    DispatchQueue.main.async { self.closeMenu() }
                    return false
                }
                menuOpen = false  // route this click normally below
                DispatchQueue.main.async { self.closeMenu() }
            }
            if let index = buttonIndexAt(x, y) {
                swallowMouseUp = true
                let target = targets[index]
                // The arrange button has no default action — a left-click
                // opens its flyout.
                let menu = target?.kind == "arrange"
                DispatchQueue.main.async { self.dispatchButton(index, target, menu: menu) }
                return false  // consumed: don't let Mission Control see it
            }
            // Any other left-click dismisses Mission Control (or starts a
            // drag). The exit zoom begins before AX shows any change, so hide
            // NOW and LATCH. The event passes through untouched.
            Log.geom(String(format: "EXIT-HIDE click at (%.0f,%.0f)", x, y))
            DispatchQueue.main.async { self.latchExit() }
        case .rightMouseDown:
            if menuOpen {
                swallowRightUp = true
                DispatchQueue.main.async { self.closeMenu() }
                return false
            }
            if let index = buttonIndexAt(x, y), let target = targets[index],
               target.kind == "quit" || target.kind == "arrange" {
                swallowRightUp = true
                DispatchQueue.main.async { self.dispatchButton(index, target, menu: true) }
                return false
            }
        case .leftMouseUp:
            if swallowMouseUp {
                swallowMouseUp = false
                return false
            }
        case .rightMouseUp:
            if swallowRightUp {
                swallowRightUp = false
                return false
            }
        default:
            break
        }
        return true
    }

    /// W / M / Q with the pointer over a window thumbnail: the same deferred
    /// actions as the buttons (press again to take it back). Any modifier, a
    /// repeat, an open menu, or no thumbnail lets the key through.
    func tapKey(_ event: CGEvent, _ x: Double, _ y: Double) -> Bool {
        let modifiers: CGEventFlags = [.maskCommand, .maskControl, .maskAlternate, .maskShift]
        if !event.flags.intersection(modifiers).isEmpty || menuOpen { return true }
        if event.getIntegerValueField(.keyboardEventAutorepeat) != 0 { return true }
        guard let action = Tuning.shortcutKeys[event.getIntegerValueField(.keyboardEventKeycode)],
              let thumb = thumbAt(x, y) else { return true }
        let title = thumb.title
        DispatchQueue.main.async { self.shortcutAction(action, title) }
        return false
    }

    /// The window thumbnail under the pointer that currently shows buttons.
    func thumbAt(_ x: Double, _ y: Double) -> Tile? {
        guard active else { return nil }
        let titles = Set(targets.values.filter { $0.kind == "window" || $0.kind == "cancel" }.map { $0.title })
        return lastThumbs.first { $0.rect.contains(x, y) && titles.contains($0.title) }
    }

    /// Queue (or, pressed again, take back) a deferred window action.
    func shortcutAction(_ action: String, _ title: String) {
        if let pending = deferredAction(for: title), pending.action == action {
            cancelDeferredAction(title)
            return
        }
        Log.geom("CLICK shortcut \(action) on '\(title)'")
        deferWindowAction(action, title)
    }

    func buttonIndexAt(_ x: Double, _ y: Double) -> Int? {
        rects.first { $0.value.contains(x, y) }?.key
    }

    // MARK: - hover
    // Tracking areas never fire while Mission Control owns the mouse, so the
    // cursor position is polled instead.

    func startHoverPolling() {
        guard hoverTimer == nil else { return }
        let timer = Timer(timeInterval: Tuning.hoverInterval, target: self, selector: #selector(hoverTick(_:)),
                          userInfo: nil, repeats: true)
        timer.tolerance = Tuning.hoverInterval * 0.1
        RunLoop.main.add(timer, forMode: .common)
        hoverTimer = timer
    }

    func stopHoverPolling() {
        hoverTimer?.invalidate()
        hoverTimer = nil
        buttons.forEach { $0.setHovered(false) }
        hoverIndex = nil
        hoverQuitIndex = nil
        hoverQuitSince = 0
    }

    @objc func hoverTick(_ timer: Timer?) {
        checkSceneTransform()
        let location = NSEvent.mouseLocation  // global, bottom-left origin
        let x = Double(location.x)
        let yTop = (screenHeight ?? 0) - Double(location.y)
        lastMouseY = yTop
        let nearBar = yTop <= Tuning.barRegionHeight
        if nearBarPrev == nil {
            nearBarPrev = nearBar
        } else if nearBar != nearBarPrev {
            // Crossing the Spaces Bar region: burst to full fast frames so its
            // buttons follow the bar's own animation.
            nearBarPrev = nearBar
            if active && settledThumbsSig != nil {
                barBurstUntil = Clock.now() + Tuning.barBurst
                watchSig = nil
                setSyncInterval(Tuning.syncIntervalFast)
            }
        }
        if menuOpen && menuKind == "dock" {
            // Sliding along the Dock moves the bubble to the icon now under
            // the pointer right away, like a menu bar.
            if let other = dockItemAt(x, yTop), other.title != menuTitle,
               !(menuPanelRect?.contains(x, yTop) ?? false) {
                closeMenu()
                dockLatched = nil
                dockHover = other.title
                dockHoverSince = Clock.now() - Tuning.quitHoverDelay
                updateDockHover(x, yTop)
                return
            }
        }
        if menuOpen {
            menuView?.setHighlight(menuRowAt(x, yTop) ?? -1)
            if let i = hoverIndex, i < buttons.count { buttons[i].setHovered(false) }
            hoverIndex = nil
            maybeAutocloseMenu(x, yTop)
            return
        }
        let hit = buttonIndexAt(x, yTop)
        // Only message buttons on hover TRANSITIONS.
        if hit != hoverIndex {
            if let i = hoverIndex, i < buttons.count { buttons[i].setHovered(false) }
            if let i = hit, i < buttons.count { buttons[i].setHovered(true) }
            hoverIndex = hit
        }
        updateQuitHover(hit)
        if hit == nil { updateDockHover(x, yTop) }
    }

    /// Open a button's flyout when the cursor dwells on it.
    func updateQuitHover(_ hit: Int?) {
        guard let hit = hit, let kind = targets[hit]?.kind, kind == "quit" || kind == "arrange" else {
            hoverQuitIndex = nil
            hoverQuitSince = 0
            return
        }
        if hoverQuitIndex != hit {
            hoverQuitIndex = hit  // just arrived; start the dwell clock
            hoverQuitSince = Clock.now()
            return
        }
        // A zeroed clock means the dwell already fired (or is suppressed just
        // after a close) — wait for the cursor to leave first.
        if hoverQuitSince != 0 && Clock.now() - hoverQuitSince >= Tuning.quitHoverDelay {
            hoverQuitSince = 0
            openMenuFor(hit, viaHover: true)
        }
    }

    /// A hover-opened menu closes itself once the cursor rests away from both
    /// its button and the menu for a moment.
    func maybeAutocloseMenu(_ x: Double, _ y: Double) {
        guard menuViaHover else { return }
        if menuHot(x, y) {
            menuAwaySince = 0
            return
        }
        if menuAwaySince == 0 {
            menuAwaySince = Clock.now()
        } else if Clock.now() - menuAwaySince >= Tuning.quitHoverCloseDelay {
            closeMenu()
        }
    }

    func menuHot(_ x: Double, _ y: Double, slop: Double = 6) -> Bool {
        [menuAnchorRect, menuPanelRect].contains { r in
            guard let r = r else { return false }
            return r.x - slop <= x && x <= r.x + r.w + slop && r.y - slop <= y && y <= r.y + r.h + slop
        }
    }

    // MARK: - flyout menus

    @objc func openMenuFor(_ index: Int, viaHover: Bool = false) {
        guard let target = targets[index] else { return }
        if target.kind == "quit" {
            openMenu(index, "quit", UI.quitMenuItems, Tuning.quitMenuActions, viaHover: viaHover)
        } else if target.kind == "arrange" {
            if noMaxTitles.contains(target.title) {
                // Fixed-size window: Maximize can't do anything.
                openMenu(index, "arrange", Array(UI.arrangeMenuItems.prefix(2)),
                         Array(Tuning.arrangeMenuActions.prefix(2)), viaHover: viaHover)
            } else {
                openMenu(index, "arrange", UI.arrangeMenuItems, Tuning.arrangeMenuActions, viaHover: viaHover)
            }
        }
    }

    func openMenu(_ index: Int?, _ kind: String, _ items: [UI.MenuItem], _ actions: [String],
                  viaHover: Bool = false, anchor: Rect? = nil, title anchorTitle: String? = nil) {
        var rect: Rect
        var title: String?
        if let anchor = anchor {
            rect = anchor  // a Dock icon, not a button
            title = anchorTitle
        } else {
            guard let index = index, let entry = targets[index], let r = rects[index], entry.kind == kind else {
                return
            }
            rect = r
            title = entry.title
        }
        menuKind = kind
        let caption = kind == "dock" ? title : nil
        let width: Double
        if kind == "dock" {
            let longest = items.map { Double(UI.captionWidth($0.label)) }.max() ?? 0
            width = max(Tuning.dockMenuWidth, Double(UI.captionWidth(caption)) + 36, 42 + longest + 16)
        } else {
            width = Double(UI.menuWidth)
        }
        let height = Double(UI.menuHeight(items.count, caption: caption))
        let sh = screenHeight ?? 0
        // Keep the menu on the display its button is on.
        let d = screenFor(Tile(x: rect.x, y: rect.y, width: rect.w, height: rect.h))
            ?? Rect(0, 0, screenWidth ?? 0, sh)
        let mx: Double
        var myTop: Double
        if kind == "dock" {
            // Centred ABOVE the Dock's own app-name label; the column down to
            // the icon counts as part of the menu for hover.
            mx = max(d.x + 2, min(rect.x + rect.w / 2 - width / 2, d.x + d.w - width - 2))
            myTop = max(d.y + 2, rect.y - Tuning.dockMenuBottomGap - height)
            let bridgeTop = myTop + height
            rect = Rect(mx, bridgeTop, width, rect.y + rect.h - bridgeTop)
        } else {
            // Just under the button, kept fully on screen.
            mx = max(d.x + 2, min(rect.x, d.x + d.w - width - 2))
            myTop = rect.y + rect.h + 2
            if myTop + height > d.y + d.h { myTop = max(d.y + 2, rect.y - height - 2) }
        }
        menuRects = [:]
        for i in 0..<actions.count {
            menuRects[i] = Rect(mx, myTop + Double(UI.menuPad) + Double(i) * Double(UI.menuRowHeight),
                                width, Double(UI.menuRowHeight))
        }
        menuTitle = title
        menuActions = actions
        menuAnchorRect = rect
        menuPanelRect = Rect(mx, myTop, width, height)
        menuViaHover = viaHover
        menuAwaySince = 0
        let panel = menuPanelEnsure()
        menuView?.setItems(items, caption: caption)
        menuView?.setHighlight(-1)
        let y = sh - myTop - height  // flip to bottom-left origin
        panel.setFrame(NSRect(x: mx, y: y, width: width, height: height), display: true)
        menuView?.frame = NSRect(x: 0, y: 0, width: width, height: height)
        menuOpen = true
        panel.orderFrontRegardless()
        Log.geom(String(format: "MENU(%@) open for '%@' at (%.0f,%.0f)", kind, title ?? "", mx, myTop))
    }

    func menuPanelEnsure() -> NSPanel {
        if let panel = menuPanel { return panel }
        let frame = NSRect(x: 0, y: 0, width: UI.menuWidth, height: UI.menuHeight(UI.quitMenuItems.count))
        let panel = makeOverlayPanel(frame, level: popUpLevel + 1, ignoresMouse: false)
        panel.hasShadow = true  // a menu casts a shadow, unlike the buttons
        let (root, view) = UI.makeMenu(frame)
        panel.contentView = root
        menuPanel = panel
        menuView = view
        return panel
    }

    func menuRowAt(_ x: Double, _ y: Double) -> Int? {
        menuRects.first { $0.value.contains(x, y) }?.key
    }

    func menuPick(_ row: Int) {
        let action = row >= 0 && row < menuActions.count ? menuActions[row] : nil
        let title = menuTitle
        let kind = menuKind, item = menuDock
        closeMenu()
        if kind == "dock" {
            if let action = action, let item = item { queueDockAction(action, item) }
            return
        }
        if let action = action, let title = title, !title.isEmpty { deferWindowAction(action, title) }
    }

    func closeMenu() {
        if !menuOpen && menuPanel == nil { return }
        menuOpen = false
        menuTitle = nil
        menuKind = nil
        menuDock = nil
        menuRects = [:]
        menuViaHover = false
        menuAwaySince = 0
        menuAnchorRect = nil
        menuPanelRect = nil
        // Suppress an immediate hover-reopen until the cursor leaves.
        hoverQuitSince = 0
        menuView?.setHighlight(-1)
        menuPanel?.orderOut(nil)
    }

    // MARK: - Dock overlay

    func dockItemAt(_ x: Double, _ y: Double) -> Tile? {
        dockItems.first { $0.rect.contains(x, y) }
    }

    func nearDock(_ x: Double, _ y: Double, slop: Double = 40) -> Bool {
        guard !dockItems.isEmpty else { return true }  // nothing read yet: keep looking
        let minX = dockItems.map { $0.x }.min()!, maxX = dockItems.map { $0.x + $0.width }.max()!
        let minY = dockItems.map { $0.y }.min()!, maxY = dockItems.map { $0.y + $0.height }.max()!
        return minX - slop <= x && x <= maxX + slop && minY - slop <= y && y <= maxY + slop
    }

    /// Hovering a running app's Dock icon opens a small Close / Quit / Force
    /// Quit menu above it after the usual dwell.
    func updateDockHover(_ x: Double, _ y: Double) {
        let now = Clock.now()
        // Re-read the Dock (an AX walk) only while the pointer is near it.
        if dockItemsAt != 0 && !nearDock(x, y) {
            if dockHover != nil {
                dockHover = nil
                dockHoverSince = 0
            }
            return
        }
        if now - dockItemsAt > Tuning.dockRefresh {
            dockItemsAt = now
            dockItems = ax.dockAppItems()
        }
        let item = dockItemAt(x, y)
        let key = item?.title
        if key != dockLatched { dockLatched = nil }
        guard let hovered = key, let found = item, hovered != dockLatched else {
            dockHover = nil
            dockHoverSince = 0
            return
        }
        if hovered != dockHover {
            dockHover = hovered
            dockHoverSince = now
            return
        }
        if dockHoverSince != 0 && now - dockHoverSince >= Tuning.quitHoverDelay {
            dockHoverSince = 0
            dockLatched = hovered
            menuDock = found
            openMenu(nil, "dock", UI.dockMenuItems, Tuning.dockMenuActions, viaHover: true,
                     anchor: found.rect, title: found.title)
        }
    }

    /// Close-all / quit / force-quit the app when Mission Control exits; mark
    /// its windows meanwhile.
    func queueDockAction(_ action: String, _ item: Tile) {
        let pid = item.bundle.flatMap { ax.pidForBundle($0) } ?? ax.pidsNamed(item.title).first
        Log.geom("DOCK \(action) '\(item.title)' pid=\(pid.map(String.init) ?? "nil")")
        guard let pid = pid else { return }
        dockActions.removeAll { $0.pid == pid }
        dockActions.append(DockAction(action: action, pid: pid, name: item.title))
        let glyph = Tuning.dockGlyphs[action] ?? "⏻"
        for thumb in lastThumbs where !thumb.title.isEmpty {
            if (item.bundle != nil && thumb.bundle == item.bundle) || ax.pidForTitle(thumb.title) == pid {
                marked[thumb.title] = glyph
                dockMarked[thumb.title] = item.title
            }
        }
        watchSig = nil  // show the marks on the next frame
    }

    @objc func applyDockActions() {
        guard !dockActions.isEmpty else { return }
        if ax.missionControlGroup() != nil {
            schedule(#selector(applyDockActions), after: 0.4)
            return
        }
        let actions = dockActions
        dockActions = []
        for item in actions {
            let ok: Bool
            switch item.action {
            case "forcequit": ok = ax.forceQuitPid(item.pid)
            case "closeall": ok = ax.closeAllWindows(item.pid) > 0
            default: ok = ax.quitPid(item.pid)
            }
            Log.geom("DOCK apply \(item.action) '\(item.name)' -> \(ok)")
        }
    }
}
