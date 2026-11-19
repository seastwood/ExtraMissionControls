import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

extension MissionControlController {
    // MARK: - dispatch

    /// Reject queued clicks if a sync hid or reassigned their panel.
    func dispatchButton(_ index: Int, _ target: ButtonTarget?, menu: Bool) {
        guard active, rects[index] != nil, let target = target, targets[index] == target else { return }
        if menu {
            openMenuFor(index)
        } else {
            closeIndex(index)
        }
    }

    /// Direct button action — only reachable if window hit-testing works (it
    /// normally doesn't during Mission Control; the tap handles it).
    @objc func closeClicked(_ sender: NSButton) { closeIndex(sender.tag) }

    @objc func closeIndex(_ index: Int) {
        guard let target = targets[index], !target.title.isEmpty else { return }
        let kind = target.kind, title = target.title, element = target.element
        // Whatever follows must show on the very next frame: bust the quiet skip.
        watchSig = nil
        switch kind {
        case .cancel:
            cancelDeferredAction(title)
            return
        case .desktop:
            showDesktopFromMC()
            return
        default:
            break
        }
        Log.geom("CLICK \(kind.rawValue) on '\(title)'")
        if [.fullscreen, .unfullscreen, .makeFullscreen, .splitAll].contains(kind) {
            // Each switches spaces / runs a full-screen animation; overlapping
            // them wedged macOS 27's WindowManager. One at a time.
            if Clock.now() < fsBusyUntil {
                Log.geom("UNFULLSCREEN busy: ignored \(kind.rawValue) on '\(title)'")
                return
            }
            fsBusyUntil = Clock.now() + Tuning.fsActionGap
        }
        switch kind {
        case .fullscreen: closeFullscreen(index, title, element)
        case .splitAll: closeSplitAll(index, title, element)
        case .unfullscreen: unfullscreenTile(index, title, element)
        case .makeFullscreen: makeFullscreen(index, title, element)
        case .minimize: deferWindowAction(.minimize, title)
        // Quit the whole app (⌘Q), deferred like a close so it leaves no ghost.
        case .quit: deferWindowAction(.quit, title)
        // Closing a windowed thumbnail WHILE Mission Control is open leaves a
        // ghost thumbnail the Dock never removes, so defer it to MC exit.
        case .window: deferWindowAction(.close, title)
        // Arrange has no default action (its click opens the menu).
        case .arrange, .cancel, .desktop: break
        }
    }

    /// Queue a window action to run when Mission Control exits. The buttons
    /// vanish now and the thumbnail gets a dim scrim glyph.
    func deferWindowAction(_ action: WindowAction, _ title: String) {
        // No action can resolve an untitled window; queueing one could only
        // ever mis-target.
        guard !title.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        let caps = ax.actionCapsFor(ax.pidForTitle(title), title)
        if (action == .close && !caps.first) || (action == .minimize && !caps.second) { return }
        if title.contains(AXBridge.keySep) && ax.pinned[title] == nil { pinSameTitled(title) }
        if title.contains(AXBridge.keySep) && ax.pinned[title] == nil {
            Log.geom("REFUSED ambiguous window '\(title)'")
            return
        }
        actionTargets[title] = ax.captureWindow(title)
        actionStarted[title] = Clock.monotonic()
        watchSig = nil  // show the scrim on the very next frame
        // One pending action per thumbnail; a later pick replaces it.
        deferredActions.removeAll { $0.title == title }
        deferredActions.append(DeferredAction(action: action, title: title))
        deferredRetries[title] = nil
        marked[title] = action.markGlyph
        for (i, t) in targets where t.title == title { dropButton(i) }
        Log.geom("DEFER \(action.rawValue) window='\(title)' (applies on MC exit)")
    }

    /// Pin every thumbnail of this key's app to its real window by layout
    /// (Mission Control keeps relative layout and shapes).
    func pinSameTitled(_ key: String) {
        guard ax.windowNumbersAvailable else { return }
        let base = AXBridge.baseTitle(key)
        let group = lastThumbs.filter { AXBridge.baseTitle($0.title) == base }
        let bundle = group.compactMap { $0.bundle }.first
        guard let pid = bundle.flatMap({ ax.pidForBundle($0) }) ?? ax.pidForTitle(base) else { return }
        let appThumbs = bundle != nil ? lastThumbs.filter { $0.bundle == bundle } : group
        let windows = ax.appWindowRects(pid)
        if appThumbs.isEmpty || windows.count != appThumbs.count {
            Log.geom("PIN '\(base)': \(appThumbs.count) thumbnails vs \(windows.count) windows, not pinned")
            return
        }
        let order = AXBridge.matchLayout(appThumbs.map { $0.rect }, windows.map { $0.rect })
        for (thumb, j) in zip(appThumbs, order) where thumb.title.contains(AXBridge.keySep) {
            ax.pinned[thumb.title] = (pid, CGWindowID(windows[j].number))
        }
        Log.geom("PIN '\(base)' -> \(ax.pinned.filter { AXBridge.baseTitle($0.key) == base }.mapValues { $0.number })")
    }

    /// Take back a pending action on a marked tile/thumbnail and restore its
    /// buttons next frame.
    func cancelDeferredAction(_ title: String) {
        watchSig = nil  // shortcuts and Dock undo also bypass quiet sync
        var cancelled = false
        let app = dockMarked[title] ?? title
        if dockActions.contains(where: { $0.name == app }) {
            // ↺ on a Dock icon OR one of that app's marked windows.
            dockActions.removeAll { $0.name == app }
            for (windowTitle, owner) in dockMarked where owner == app {
                dockMarked[windowTitle] = nil
                marked[windowTitle] = nil
            }
            Log.geom("DOCK cancel '\(app)'")
            return
        }
        var kept: [DeferredAction] = []
        for item in deferredActions {
            if item.title == title || looselySame(title, item.title) {
                marked[item.title] = nil
                actionTargets[item.title] = nil
                actionStarted[item.title] = nil
                deferredRetries[item.title] = nil
                cancelled = true
            } else {
                kept.append(item)
            }
        }
        deferredActions = kept
        // A full-screen app that un-fullscreened and is queued to close.
        if let unfsApp = unfsPending.removeValue(forKey: title) {
            deferredAppCloses.removeAll { $0 == unfsApp }
            if pendingUnfsApp == unfsApp {
                pendingUnfsBaseline = nil
                pendingUnfsApp = nil
            }
            cancelled = true
        }
        // A held-reference full-screen close still in the Spaces Bar.
        if deferredClose.contains(where: { $0.title == title }) {
            deferredClose.removeAll { $0.title == title }
            cancelled = true
        }
        marked[title] = nil
        for (i, t) in targets where t.kind == .cancel && t.title == title { dropButton(i) }
        if cancelled { Log.geom("CANCEL pending action for '\(title)'") }
    }

    // MARK: - applying deferred window actions

    @objc func applyDeferredActions() {
        guard !deferredActions.isEmpty else { return }
        if ax.missionControlGroup() != nil {
            // Mission Control re-opened: wait again so we never act in place.
            schedule(#selector(applyDeferredActions), after: 0.4)
            return
        }
        let actions = deferredActions
        deferredActions = []
        // MC can return to the full-screen Space it was opened from; desktop
        // windows still exist there, but are absent from the on-screen list.
        let infos = ax.listWindows(excludePid: ownPid, allSpaces: true)
        var seen = Set<pid_t>()
        let pids = infos.map { $0.pid }.filter { seen.insert($0).inserted }
        for item in actions {
            let action = item.action, title = item.title
            let target = actionTargets[title]
            let dispatchedAt = Clock.monotonic()
            var ok: Bool
            if let target = target, action.isWindowButton {
                ok = ax.performCapturedAction(target, action)
            } else if let region = action.region {
                // Staged by processArrangeQueue: native tiling needs the app
                // frontmost, and back-to-back tiles collided.
                arrangeQueue.append((region, title))
                ok = true
            } else {
                switch action {
                case .minimize:
                    ok = ax.minimizeWindowByTitle(title, pids, foreground: true)
                        || pressHeldButton(title, infos, close: false)
                case .quit: ok = quitAppFor(title, infos)
                case .forceQuit: ok = quitAppFor(title, infos, level: .force)
                case .kill: ok = quitAppFor(title, infos, level: .kill)
                default:
                    ok = ax.closeWindowByTitle(title, pids, foreground: true)
                        || pressHeldButton(title, infos, close: true)
                }
            }
            if !ok && action.isWindowButton {
                let attempts = deferredRetries[title] ?? 0
                if attempts < 8, let pid = target?.pid ?? ax.pidForTitle(title) {
                    // Off-Space apps may hide their AX windows until activated.
                    if attempts == 0 { ax.activatePid(pid) }
                    deferredRetries[title] = attempts + 1
                    deferredActions.append(item)
                    continue
                }
            }
            deferredRetries[title] = nil
            if !ok && action.isWindowButton && target == nil && ax.pinned[title] == nil {
                // AX-opaque window (Steam etc.): fall back to a synthetic click
                // on the native traffic light. Never retarget a captured one.
                if let info = matchWindowInfo(title, ax.listWindows(excludePid: ownPid)) {
                    tlQueue.append((action, title, info.pid, info.x, info.y))
                }
            }
            let started = actionStarted.removeValue(forKey: title) ?? dispatchedAt
            actionTargets[title] = nil
            Log.geom(String(format: "ACTION-TIMING %@ '%@' queued=%.3fs dispatch=%.3fs accepted=%@", action.rawValue, title,
                            dispatchedAt - started, Clock.monotonic() - dispatchedAt, ok ? "true" : "false"))
            if ok, let target = target, action.isWindowButton {
                actionVerifications.append((target, action, title, Clock.monotonic()))
            } else if !ok {
                Log.note("could not apply \(action.rawValue) to '\(title)'")
            }
        }
        if !actionVerifications.isEmpty {
            cancelScheduled(#selector(verifyWindowActions))
            schedule(#selector(verifyWindowActions), after: 0.1)
        }
        if !deferredActions.isEmpty { schedule(#selector(applyDeferredActions), after: 0.2) }
        if !tlQueue.isEmpty { schedule(#selector(processTrafficLightQueue), after: 0.05) }
        if !arrangeQueue.isEmpty && arrangeCurrent == nil { schedule(#selector(processArrangeQueue), after: 0.05) }
    }

    @objc func verifyWindowActions() {
        var pending: [(target: CapturedWindow, action: WindowAction, title: String, started: Double)] = []
        for v in actionVerifications {
            let state = ax.capturedActionState(v.target, v.action)
            let elapsed = Clock.monotonic() - v.started
            if state == true {
                Log.geom(String(format: "ACTION-VERIFIED %@ '%@' after %.3fs", v.action.rawValue, v.title, elapsed))
            } else if elapsed < 2.0 {
                pending.append(v)
            } else {
                // A save sheet or inaccessible window is not a reason to repeat
                // a close, dismiss a dialog, or click another window.
                Log.note("\(v.action.rawValue) not confirmed for '\(v.title)' (may need attention)")
            }
        }
        actionVerifications = pending
        if !pending.isEmpty { schedule(#selector(verifyWindowActions), after: 0.2) }
    }

    /// Apply the next queued tile: focus its window now, press the native tile
    /// menu a beat later once the app has come forward.
    @objc func processArrangeQueue() {
        guard arrangeCurrent == nil, !arrangeQueue.isEmpty else { return }
        let (region, title) = arrangeQueue.removeFirst()
        let infos = ax.listWindows(excludePid: ownPid, allSpaces: true)
        var seen = Set<pid_t>()
        let pids = infos.map { $0.pid }.filter { seen.insert($0).inserted }
        let pid = ax.focusWindowByTitle(title, pids)
        arrangeCurrent = (title, pids, pid, region)
        schedule(#selector(pressArrangeStep), after: 0.35)
    }

    @objc func pressArrangeStep() {
        if let current = arrangeCurrent {
            arrangeCurrent = nil
            var ok = current.pid.map { ax.tileFocusedApp($0, current.region) } ?? false
            if !ok { ok = ax.snapWindowByTitle(current.title, current.pids, current.region) }
            Log.geom("ARRANGE \(current.region.rawValue) '\(current.title)' -> \(ok)")
        }
        if !arrangeQueue.isEmpty { schedule(#selector(processArrangeQueue), after: 0.5) }
    }

    /// Quit the app owning the thumbnail, by pid: graceful ⌘Q, force (Force
    /// Quit), or kill (SIGKILL).
    func quitAppFor(_ title: String, _ infos: [WindowInfo], level: QuitLevel = .graceful) -> Bool {
        let pid = ax.pidForTitle(title) ?? matchWindowInfo(title, infos)?.pid
        Log.geom("APPLY-DEFERRED quit(\(level.rawValue)) '\(title)' pid=\(pid.map(String.init) ?? "nil")")
        guard let pid = pid else { return false }
        switch level {
        case .force: return ax.forceQuitPid(pid)
        case .kill: return ax.killPid(pid)
        case .graceful: return ax.quitPid(pid)
        }
    }

    /// The CG window best matching a thumbnail title: exact title or owner,
    /// else a unique prefix relation.
    func matchWindowInfo(_ title: String, _ infos: [WindowInfo]) -> WindowInfo? {
        let base = AXBridge.baseTitle(title)
        if let exact = infos.first(where: { $0.title == base || $0.owner == base }) { return exact }
        let related = infos.filter { AXBridge.titlesRelated(base, $0.title) || AXBridge.titlesRelated(base, $0.owner) }
        return related.count == 1 ? related[0] : nil
    }

    @objc func processTrafficLightQueue() {
        guard let first = tlQueue.first else { return }
        // These windows ignore clicks unless active — raise, then click.
        ax.activatePid(first.pid)
        tlPending = (first.action, first.title, first.x, first.y)
        schedule(#selector(clickTrafficLight), after: 0.3)
    }

    @objc func clickTrafficLight() {
        let pending = tlPending
        tlPending = nil
        if !tlQueue.isEmpty { tlQueue.removeFirst() }
        if let p = pending {
            // Close ~x+20, minimize ~x+40, ~19px below the top edge.
            let cx = p.x + (p.action == .minimize ? 40 : 20), cy = p.y + 19
            clickAt(cx, cy)
            Log.geom(String(format: "TRAFFIC-LIGHT %@ '%@' at (%.0f,%.0f)", p.action.rawValue, p.title, cx, cy))
        }
        if !tlQueue.isEmpty { schedule(#selector(processTrafficLightQueue), after: 0.4) }
    }

    /// Close a just-un-fullscreened app's main window once Mission Control is
    /// gone, by NAME, retrying while the window un-parks.
    @objc func applyDeferredAppCloses() {
        guard let app = deferredAppCloses.first else { return }
        if ax.missionControlGroup() != nil {
            schedule(#selector(applyDeferredAppCloses), after: 0.4)
            return
        }
        if ax.closeMainWindow(app) {
            Log.geom("APPLY-APP-CLOSE '\(app)' -> closed (attempt \(appCloseAttempts))")
            finishAppClose(app)
            return
        }
        appCloseAttempts += 1
        if appCloseAttempts < 6 {
            schedule(#selector(applyDeferredAppCloses), after: 0.4)
            return
        }
        Log.geom("APPLY-APP-CLOSE '\(app)' -> AX close failed; traffic-light")
        clickAppClose(app)
        finishAppClose(app)
    }

    func finishAppClose(_ app: String) {
        deferredAppCloses.removeAll { $0 == app }
        appCloseAttempts = 0
        if !deferredAppCloses.isEmpty { schedule(#selector(applyDeferredAppCloses), after: 0.3) }
    }

    @discardableResult
    func clickAppClose(_ appName: String) -> Bool {
        guard let info = matchWindowInfo(appName, ax.listWindows(excludePid: ownPid)) else { return false }
        tlQueue.append((.close, appName, info.pid, info.x, info.y))
        schedule(#selector(processTrafficLightQueue), after: 0.05)
        return true
    }

    /// Press a window button captured before Mission Control opened: exact
    /// title, else a UNIQUE prefix match, else the pid single-window fallback.
    func pressHeldButton(_ title: String, _ infos: [WindowInfo], close: Bool) -> Bool {
        func pick(_ held: HeldWindow) -> AXUIElement? { close ? held.close : held.minimize }
        if ax.pinned[title] != nil {  // a same-titled twin: only its pinned window
            return ax.pressElement(ax.buttonOf(ax.pinnedWindow(title), close ? "AXCloseButton" : "AXMinimizeButton"))
        }
        let base = AXBridge.baseTitle(title)
        let entries = windowRegistry.values.flatMap { $0 }
        for entry in entries where entry.title == base {
            if let button = pick(entry), ax.pressElement(button) { return true }
        }
        let related = entries.filter { pick($0) != nil && AXBridge.titlesRelated(base, $0.title) }
        if related.count == 1, ax.pressElement(pick(related[0])) { return true }
        for info in infos where info.title == base {
            let held = windowRegistry[info.pid] ?? []
            if held.count == 1, let button = pick(held[0]), ax.pressElement(button) { return true }
        }
        return false
    }

    // MARK: - make full screen (⤢)

    /// A thumbnail has no AX "enter full screen" action and parked windows
    /// ignore AXFullScreen while MC is open, so AXPress the thumbnail (focus
    /// the window, leave MC), then press its full-screen button.
    func makeFullscreen(_ index: Int, _ title: String, _ element: AXUIElement?) {
        guard let element = element else { return }
        dropButton(index)
        Log.geom("MAKE-FULLSCREEN window='\(title)'")
        _ = ax.pressElement(element)
        fsMakeTitle = title
        schedule(#selector(makeFullscreenStep), after: 0.6)
    }

    @objc func makeFullscreenStep() {
        guard let title = fsMakeTitle else { return }
        // macOS 27: starting a full-screen animation while MC's exit animation
        // still runs wedged WindowManager. Wait until it's gone, then a beat.
        if AX.isMacOS27 {
            let gone = ax.missionControlGroup() == nil
            if !gone && fsMakeWaits < 20 {
                fsMakeWaits += 1
                schedule(#selector(makeFullscreenStep), after: 0.15)
                return
            }
            if !fsMakeSettled {
                fsMakeSettled = true
                schedule(#selector(makeFullscreenStep), after: 0.7)
                return
            }
            fsMakeWaits = 0
            fsMakeSettled = false
        }
        fsMakeTitle = nil
        // The thumbnail's bundle id names the app; its focused window is the
        // one AXPress just brought forward.
        if let pid = ax.pidForTitle(title), let window = ax.focusedWindow(of: pid) {
            let frame = AX.frame(of: window)
            Log.geom("MAKE-FULLSCREEN '\(title)' pid=\(pid) frame=\(String(describing: frame))")
            ax.activatePid(pid)
            if let f = frame {
                fsMakeRect = (title: title, x: f.x, y: f.y)
            } else {
                fsMakeRect = nil
            }
            fsMakeWindow = window
            if let button = AX.child(window, "AXFullScreenButton") {
                AX.press(button)
            } else {
                ax.setFullscreen(window, true)
            }
            schedule(#selector(verifyMakeFullscreenWindow), after: 2.0)
            return
        }
        guard let info = matchWindowInfo(title, ax.listWindows(excludePid: ownPid)) else {
            Log.geom("MAKE-FULLSCREEN '\(title)': no CG window match")
            return
        }
        ax.activatePid(info.pid)
        fsMakeRect = (title, info.x, info.y)
        fsMakeOwner = info.owner
        if ax.fullscreenWindowOf(info.owner) {
            schedule(#selector(verifyMakeFullscreen), after: 1.0)
        } else {
            clickFullscreenButton()
        }
    }

    @objc func verifyMakeFullscreenWindow() {
        guard let window = fsMakeWindow else { return }
        fsMakeWindow = nil
        let state = AX.bool(window, "AXFullScreen")
        let frame = AX.frame(of: window)
        Log.geom("MAKE-FULLSCREEN verify fullscreen=\(String(describing: state)) frame=\(String(describing: frame))")
        if state == true {
            fsMakeRect = nil
            return
        }
        // macOS 27: no blind green-button click (traffic lights sit in
        // app-specific spots). One AX retry instead.
        if AX.isMacOS27 {
            fsMakeRect = nil
            if AX.alive(window) {
                Log.geom("MAKE-FULLSCREEN retry via AXFullScreen ok=\(ax.setFullscreen(window, true))")
            }
            return
        }
        // A screen-sized frame means a transition is still in flight; never
        // click blind then.
        if let frame = frame, let sw = screenWidth, frame.w >= 0.95 * sw {
            fsMakeRect = nil
            return
        }
        if let rect = fsMakeRect, let frame = frame {
            fsMakeRect = (rect.title, frame.x, frame.y)
            clickFullscreenButton()
        }
    }

    @objc func verifyMakeFullscreen() {
        guard let data = fsMakeRect else { return }
        if ax.findFullscreenWindow(fsMakeOwner) != nil || ax.findFullscreenWindowByTitle(data.title) != nil {
            fsMakeRect = nil
            Log.geom("MAKE-FULLSCREEN '\(data.title)' via AX button")
            return
        }
        Log.geom("MAKE-FULLSCREEN '\(data.title)' AX no-op; clicking green button")
        clickFullscreenButton()
    }

    @objc func clickFullscreenButton() {
        guard let data = fsMakeRect else { return }
        fsMakeRect = nil
        // The green button is the third traffic light: ~x+60, ~19px down.
        let cx = data.x + 60, cy = data.y + 19
        clickAt(cx, cy)
        Log.geom(String(format: "MAKE-FULLSCREEN click '%@' at (%.0f,%.0f)", data.title, cx, cy))
    }

    // MARK: - Spaces Bar tiles (through macOS 26)

    /// Exit full screen for a Spaces Bar tile without closing the app.
    func unfullscreenTile(_ index: Int, _ title: String, _ element: AXUIElement?) {
        guard let element = element else { return }
        dropButton(index)
        if dropToDesktop(element) {
            Log.geom("UNFULLSCREEN tile='\(title)'")
            recentlyClosed["acted|\(title)"] = Clock.now() + 2.5
        } else {
            Log.note("could not exit full screen for '\(title)'")
        }
    }

    /// Close BOTH apps of a Split View tile (✕2): switch to the split space,
    /// close the first app; the survivor turns solo and is closed next.
    func closeSplitAll(_ index: Int, _ title: String, _ element: AXUIElement?) {
        guard let element = element else {
            Log.note("cannot reach split '\(title)'")
            return
        }
        dropButton(index)
        recentlyClosed["acted|\(title)"] = Clock.now() + 6.0
        let apps = title.components(separatedBy: " & ").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        Log.geom("SPLIT-CLOSE-ALL tile='\(title)' apps=\(apps)")
        guard let first = apps.first else { return }
        afterCloseQueue = Array(apps.dropFirst())
        pendingCloseTitle = first
        pendingCloseAttempts = 10
        chainClickFirst = true
        AX.press(element)  // switch to the split space; MC exits
        schedule(#selector(finishPendingClose), after: 0.6)
    }

    func pendingChainDone() {
        if !afterCloseQueue.isEmpty {
            let next = afterCloseQueue.removeFirst()
            Log.geom("  chain: closing next split member '\(next)'")
            pendingCloseTitle = next
            pendingCloseAttempts = 8
            schedule(#selector(finishPendingClose), after: 0.7)
        } else {
            chainClickFirst = false
            schedule(#selector(reopenMissionControl), after: 0.6)
        }
    }

    func pendingChainFailed(_ title: String) {
        if !afterCloseQueue.isEmpty {
            Log.geom("  chain: '\(title)' failed; abandoning \(afterCloseQueue)")
            afterCloseQueue = []
        }
        chainClickFirst = false
        schedule(#selector(reopenMissionControl), after: 1.0)
    }

    /// Close a full-screen app from the Spaces Bar: drop it back to the
    /// desktop (so it stays in Mission Control as a thumbnail), then mark THAT
    /// thumbnail for a deferred close — no ghost.
    func closeFullscreen(_ index: Int, _ title: String, _ element: AXUIElement?) {
        let entry = fullscreenRegistry[title]
        dropButton(index)
        if let element = element, dropToDesktop(element) {
            Log.geom("CLOSE-FULLSCREEN '\(title)': un-fullscreened, deferring close")
            recentlyClosed["acted|\(title)"] = Clock.now() + 2.5
            fullscreenRegistry[title] = nil
            // Scrim whichever thumbnail appears that isn't in this baseline.
            pendingUnfsBaseline = prevThumbTitles
            pendingUnfsExpires = Clock.now() + 4.0
            pendingUnfsApp = title
            // Reliable close once MC exits: the app's MAIN window BY NAME.
            if !deferredAppCloses.contains(title) { deferredAppCloses.append(title) }
            return
        }
        if let entry = entry {
            Log.geom("CLOSE-FULLSCREEN '\(title)': no un-fullscreen; held deferred close")
            deferredClose.removeAll { $0.title == title }
            deferredClose.append((title, entry))
            marked[title] = "✕"
            return
        }
        fallbackCloseFullscreen(title, element)
    }

    func fallbackCloseFullscreen(_ title: String, _ element: AXUIElement?) {
        guard let element = element else {
            Log.note("no closable fullscreen named '\(title)'")
            return
        }
        // Switch to its space (MC closes, the app becomes AX-visible), close
        // it there, then re-enter Mission Control.
        Log.debug("fallback: switching to space '\(title)' to close it")
        pendingCloseTitle = title
        pendingCloseAttempts = 10
        chainClickFirst = false
        AX.press(element)
        schedule(#selector(finishPendingClose), after: 0.6)
    }

    /// Through macOS 26: the Spaces Bar's own AXRemoveDesktop turns a
    /// full-screen space back into an ordinary window, MC staying open.
    func dropToDesktop(_ element: AXUIElement) -> Bool { ax.removeSpace(element) }

    // MARK: - Show Desktop

    /// Leave Mission Control, then Show Desktop (asked for while MC is up,
    /// macOS only closes MC).
    func showDesktopFromMC() {
        latchExit()
        desktopWait = 0
        toggleMissionControl()
        schedule(#selector(showDesktopAfterExit), after: 0.15)
    }

    @objc func showDesktopAfterExit() {
        if ax.missionControlGroup() != nil {
            desktopWait += 1
            if desktopWait < 30 {
                schedule(#selector(showDesktopAfterExit), after: 0.1)
            } else {
                Log.geom("DESKTOP gave up: Mission Control never closed")
            }
            return
        }
        desktopBefore = ax.appWindowFrames()
        let sent = ax.showDesktop(viaDock: true)
        Log.geom("DESKTOP dock sent=\(sent) windows=\(desktopBefore.count)")
        schedule(#selector(showDesktopVerify), after: 0.8)
    }

    /// No window moved → the Dock request didn't take: press the shortcut.
    @objc func showDesktopVerify() {
        let after = ax.appWindowFrames()
        let moved = desktopBefore.filter { after[$0.key] != $0.value }.count
        Log.geom("DESKTOP verify moved=\(moved)/\(desktopBefore.count)")
        if !desktopBefore.isEmpty && moved == 0 {
            Log.geom("DESKTOP shortcut sent=\(ax.showDesktop(viaDock: false))")
        }
    }

    /// Only open if Mission Control is closed — the toggle would close it.
    @objc func reopenMissionControl() {
        if ax.missionControlGroup() == nil { toggleMissionControl() }
    }

    func toggleMissionControl() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-b", "com.apple.exposelauncher"]
        try? task.run()
    }

    // MARK: - pending fullscreen close chain

    @objc func finishPendingClose() {
        guard let title = pendingCloseTitle else { return }
        guard let window = ax.findFullscreenTarget(title) else {
            // The space switch may still be animating.
            pendingCloseAttempts -= 1
            if pendingCloseAttempts > 0 {
                schedule(#selector(finishPendingClose), after: 0.35)
            } else {
                pendingCloseTitle = nil
                Log.note("gave up closing fullscreen '\(title)'")
                pendingChainFailed(title)
            }
            return
        }
        ax.focusWindow(window)
        pendingCloseWindow = window
        if chainClickFirst, let frame = AX.frame(of: window) {
            // Split members ignore AX close presses — go straight for the real
            // traffic light. Start the title-bar reveal now.
            Log.geom(String(format: "  finishPendingClose: '%@' — traffic-light first (x=%.0f)", title, frame.x))
            postMouse(.mouseMoved, frame.x + 20, 5)
            schedule(#selector(clickPendingTrafficLight), after: 0.55)
            return
        }
        Log.geom("  finishPendingClose: focusing '\(title)', then pressing close")
        schedule(#selector(pressPendingClose), after: 0.25)
    }

    @objc func pressPendingClose() {
        let window = pendingCloseWindow
        pendingCloseWindow = nil
        guard let title = pendingCloseTitle, let win = window else { return }
        if let button = AX.child(win, "AXCloseButton") {
            AX.press(button)
        } else {
            _ = ax.closeFullscreenWindow(title)
        }
        schedule(#selector(verifyPendingClose), after: 0.6)
    }

    @objc func verifyPendingClose() {
        guard let title = pendingCloseTitle else { return }
        guard let window = ax.findFullscreenTarget(title) else {
            Log.geom("  verifyPendingClose: '\(title)' closed by close-button press")
            pendingCloseTitle = nil
            fullscreenRegistry[title] = nil
            pendingChainDone()
            return
        }
        // The AX press no-oped (split halves ignore it): click the REAL
        // traffic light, revealing the title bar first.
        if let frame = AX.frame(of: window) {
            Log.geom(String(format: "  verifyPendingClose: '%@' still fullscreen; clicking its traffic light (x=%.0f)",
                            title, frame.x))
            pendingCloseWindow = window
            postMouse(.mouseMoved, frame.x + 20, 5)
            schedule(#selector(clickPendingTrafficLight), after: 0.55)
            return
        }
        escalateUnfullscreen(title, window)
    }

    @objc func clickPendingTrafficLight() {
        let window = pendingCloseWindow
        pendingCloseWindow = nil
        guard pendingCloseTitle != nil, let win = window else { return }
        let x = (AX.frame(of: win)?.x ?? 0) + 20
        // The revealed title-bar strip occupies the top ~33pt of the screen.
        clickAt(x, 16)
        schedule(#selector(verifyTrafficLightClose), after: 0.7)
    }

    @objc func verifyTrafficLightClose() {
        guard let title = pendingCloseTitle else { return }
        guard let window = ax.findFullscreenTarget(title) else {
            Log.geom("  verifyTrafficLightClose: '\(title)' closed by traffic light")
            pendingCloseTitle = nil
            fullscreenRegistry[title] = nil
            pendingChainDone()
            return
        }
        Log.geom("  verifyTrafficLightClose: '\(title)' survived; un-fullscreening")
        escalateUnfullscreen(title, window)
    }

    /// Last resort: un-fullscreen, then close the resulting normal window.
    func escalateUnfullscreen(_ title: String, _ window: AXUIElement) {
        pendingCloseTitle = nil
        Log.debug("un-fullscreening '\(title)' to close it")
        if ax.setFullscreen(window, false) {
            pendingCloseExitTitle = title
            schedule(#selector(finishExitedClose), after: 1.9)
        } else {
            Log.note("could not close fullscreen '\(title)'")
            pendingChainFailed(title)
        }
    }

    @objc func finishExitedClose() {
        guard let title = pendingCloseExitTitle else { return }
        pendingCloseExitTitle = nil
        if ax.closeMainWindow(title) {
            Log.geom("  finishExitedClose: closed '\(title)''s un-fullscreened window")
            fullscreenRegistry[title] = nil
            pendingChainDone()
        } else {
            Log.note("un-fullscreened '\(title)' but could not close its window")
            pendingChainFailed(title)
        }
    }

    // MARK: - held-reference fullscreen closes (applied after MC exits)

    @objc func processDeferredCloses() {
        guard !deferredClose.isEmpty else { return }
        let (title, entry) = deferredClose.removeFirst()
        var window: AXUIElement? = entry.window
        // The element can be recreated by the fullscreen transition.
        if !AX.alive(window) { window = ax.findAppWindow(title, held: entry.window, title: entry.title) }
        guard let win = window else {
            Log.debug("deferred close of '\(title)': already gone")
            nextDeferredClose()
            return
        }
        AX.press(AX.child(win, "AXCloseButton") ?? entry.close)
        deferredVerifyTitle = title
        schedule(#selector(verifyDeferredClose), after: 0.8)
    }

    @objc func verifyDeferredClose() {
        guard let title = deferredVerifyTitle else { return }
        deferredVerifyTitle = nil
        guard let window = ax.findFullscreenTarget(title) else {
            Log.debug("deferred close of '\(title)' completed")
            fullscreenRegistry[title] = nil
            nextDeferredClose()
            return
        }
        // The press was ignored while fullscreen: un-fullscreen, then close.
        if ax.setFullscreen(window, false) {
            deferredExitTitle = title
            schedule(#selector(finishDeferredExitClose), after: 1.9)
        } else {
            Log.note("could not finish closing '\(title)'")
            nextDeferredClose()
        }
    }

    @objc func finishDeferredExitClose() {
        guard let title = deferredExitTitle else { return }
        deferredExitTitle = nil
        if ax.closeMainWindow(title) {
            Log.debug("deferred close of '\(title)' completed after un-fullscreen")
            fullscreenRegistry[title] = nil
        } else {
            Log.note("un-fullscreened '\(title)' but could not close it")
        }
        nextDeferredClose()
    }

    func nextDeferredClose() {
        if !deferredClose.isEmpty { schedule(#selector(processDeferredCloses), after: 0.3) }
    }

    // MARK: - synthetic input

    func postMouse(_ type: CGEventType, _ x: Double, _ y: Double) {
        let event = CGEvent(mouseEventSource: nil, mouseType: type,
                            mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left)
        event?.setIntegerValueField(.eventSourceUserData, value: Tuning.syntheticMouseTag)
        event?.post(tap: .cghidEventTap)
        usleep(60_000)
    }

    func clickAt(_ x: Double, _ y: Double) {
        postMouse(.mouseMoved, x, y)
        postMouse(.leftMouseDown, x, y)
        postMouse(.leftMouseUp, x, y)
    }
}
