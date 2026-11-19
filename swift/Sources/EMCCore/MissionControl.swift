import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

/// Float macOS-style controls over the real Mission Control (a port of
/// mission_control.py).
///
/// Two macOS quirks shape the design:
///
/// 1. Input: while Mission Control is up, WindowServer routes mouse events to
///    the Dock even though our panels render on top, so window hit-testing
///    never sees clicks. A CGEventTap claims clicks on our button rects (only
///    those) before the Dock gets them.
///
/// 2. Fullscreen windows: apps stop reporting AX windows while Mission Control
///    is open, and most report only current-space windows even normally. So
///    an accumulative registry of windows (scanned while Mission Control is
///    closed) holds AX references that stay pressable from anywhere.
enum Tuning {
    static let dozeInterval = 5.0       // detect poll while the displays sleep
    static let watchdogInterval = 1.0   // detect poll while MC is up
    static let syncStall = 3.0          // no sync tick this long while active => reset
    static let detectInterval = 0.15    // Mission Control open/close detection
    static let syncInterval = 0.1       // button re-positioning while MC is active
    /// Until a session's FIRST settle the sync loop runs faster.
    static let syncIntervalFast = 0.05
    static let hoverInterval = 1.0 / 30.0
    static let fadeIn = 0.15
    static let fadeOut = 0.08
    static let quitHoverDelay = 0.4      // dwell on a menu button before it opens
    static let quitHoverCloseDelay = 0.35
    static let registryMinGap = 1.0
    /// A probable-exit latch may only hold this long (real exits deactivate
    /// within ~0.3-0.7s; a latch still alive with MC up was a false positive).
    static let exitLatchMax = 0.8
    /// A thumbs layout is trusted at the bar's commit snap, or after holding
    /// still this long (longer when the bar rests collapsed).
    static let settleConfirm = 0.4
    static let settleConfirmNoBar = 1.5
    /// Raw trackpad touch frames (NSEventTypeGesture) stream at ~125Hz while
    /// a finger touches the pad; none for touchIdle ⇒ fingers are off.
    static let touchEventType: UInt32 = 29
    static let touchIdle = 0.12
    static let barRegionHeight = 160.0
    static let barBurst = 0.8
    static let quietResync = 1.0
    static let quietResyncBar = 0.25
    /// The scene gate opens after this many consecutive at-rest probe reads.
    static let sceneStreakMin = 3
    static let sceneDeadTicksMax = 90
    static let sceneClosedTicksMax = 900
    static let buttonInset = 4.0
    static let buttonGap = 5.0
    static let desktopMargin = 16.0
    static let dockPad = 14.0
    static let minButtonSize = 14.0
    /// The Spaces Bar is placeable only with full-height tiles at positive y.
    static let minBarTileHeight = 50.0
    static let dockRefresh = 1.5
    static let dockMenuBottomGap = 6.0
    static let dockMenuWidth = 128.0
    static let fsActionGap = 3.0
    static let cornerProbeSize = 4.0
    /// Identifies only our own posted mouse events.
    static let syntheticMouseTag: Int64 = 0x454D_434D

    static let quitMenuActions = ["quit", "forcequit", "kill"]
    static let arrangeMenuActions = ["snapleft", "snapright", "snapmax"]
    static let dockMenuActions = ["closeall", "quit", "forcequit"]
    static let dockGlyphs = ["closeall": "✕", "quit": "⏻", "forcequit": "⏼"]
    /// W close, M minimize, Q quit (ANSI key codes).
    static let shortcutKeys: [Int64: String] = [13: "close", 46: "minimize", 12: "quit"]
    static let markGlyphs = ["minimize": "−", "quit": "⏻", "forcequit": "⏼", "kill": "⭘",
                             "snapleft": "◧", "snapright": "◨", "snapmax": "■"]

    /// macOS 27: two tiny corner probes replace the screen-sized scene probe,
    /// which WindowManager painted black into every Spaces Bar preview.
    static var splitSceneProbe: Bool { AX.isMacOS27 }
    /// Spaces Bar tile buttons only through macOS 26 (on 27 the only in-MC
    /// action is macOS's own hover button, invisible to AX).
    static var barButtons: Bool { !AX.isMacOS27 }
    /// macOS 27's WindowManager draws an uncapturable window as solid black in
    /// space previews, so there the panels stay capturable (the probe is
    /// alpha 0, so previews look right).
    static var panelSharing: NSWindow.SharingType { AX.isMacOS27 ? .readOnly : .none }
}

/// What a pooled button panel currently acts on.
struct ButtonTarget: Equatable {
    var kind: String
    var title: String
    var element: AXUIElement?

    static func == (a: ButtonTarget, b: ButtonTarget) -> Bool {
        guard a.kind == b.kind, a.title == b.title else { return false }
        switch (a.element, b.element) {
        case (nil, nil): return true
        case let (x?, y?): return CFEqual(x, y)
        default: return false
        }
    }
}

struct DeferredAction: Equatable {
    var action: String
    var title: String
}

struct PanelKey: Hashable {
    var kind: String
    var title: String
    var slot: Int
    var x: Int
    var y: Int
}

struct TileKey: Hashable {
    var title: String
    var x: Int
    var y: Int
}

struct DockAction {
    var action: String
    var pid: pid_t
    var name: String
}

class MissionControlController: NSObject {
    let ax: AXBridge

    // -- panels ----------------------------------------------------------------
    var panels: [NSPanel] = []
    var fadeTarget: [ObjectIdentifier: Int] = [:]
    var buttons: [CloseButton] = []
    var targets: [Int: ButtonTarget] = [:]
    var rects: [Int: Rect] = [:]
    var panelKeys: [PanelKey: Int] = [:]
    var buttonApplied: [Int: (label: String, hover: RGB, size: Double)] = [:]
    var markPanels: [NSPanel] = []
    var markKeys: [TileKey: Int] = [:]
    var markApplied: [Int: [Double]] = [:]
    var trayPanels: [NSPanel] = []
    var trayKeys: [TileKey: Int] = [:]
    var trayApplied: [Int: [Double]] = [:]

    // -- session ---------------------------------------------------------------
    var geomSignature: String?
    var prevLayout: (spaces: [RoundedRect], thumbs: [RoundedRect])?
    var screenWidth: Double?
    var screenHeight: Double?
    var screenRects: [Rect] = []
    var active = false
    var mcGroup: AXUIElement?
    var syncInterval = 0.0
    var detectTimer: Timer?
    var dozing = false
    var lastSyncTick = 0.0
    var syncTimer: Timer?
    var hoverTimer: Timer?
    var activatedAt = 0.0

    // -- event taps --------------------------------------------------------------
    var tap: CFMachPort?
    var tapSource: CFRunLoopSource?
    var keyTap: CFMachPort?
    var keyTapSource: CFRunLoopSource?
    var keyTapFailed = false
    var swallowMouseUp = false
    var swallowRightUp = false
    var swallowOtherUp = false

    // -- registries ----------------------------------------------------------------
    /// {app name: fullscreen window} accumulated across space visits.
    var fullscreenRegistry: [String: FullscreenEntry] = [:]
    /// {pid: held window buttons} for the current space, rebuilt each scan.
    var windowRegistry: [pid_t: [HeldWindow]] = [:]
    var runningNames: Set<String> = []
    var lastRegistryScan = 0.0
    var capsRefreshedAt = 0.0

    // -- pending fullscreen closes -----------------------------------------------
    var pendingCloseTitle: String?
    var pendingCloseAttempts = 0
    var pendingCloseWindow: AXUIElement?
    var pendingCloseExitTitle: String?
    var afterCloseQueue: [String] = []
    var chainClickFirst = false
    var deferredClose: [(title: String, entry: FullscreenEntry)] = []
    var deferredVerifyTitle: String?
    var deferredExitTitle: String?
    /// (kind, title) -> deadline; keeps a just-acted target's buttons from
    /// flashing back during Mission Control's re-layout animation.
    var recentlyClosed: [String: Double] = [:]

    // -- deferred window actions -----------------------------------------------
    var deferredActions: [DeferredAction] = []
    var deferredRetries: [String: Int] = [:]
    var actionTargets: [String: CapturedWindow] = [:]
    var actionStarted: [String: Double] = [:]
    var actionVerifications: [(target: CapturedWindow, action: String, title: String, started: Double)] = []
    /// Synthetic traffic-light clicks for AX-opaque windows.
    var tlQueue: [(action: String, title: String, pid: pid_t, x: Double, y: Double)] = []
    var tlPending: (action: String, title: String, x: Double, y: Double)?
    var deferredAppCloses: [String] = []
    var appCloseAttempts = 0
    var lastThumbs: [Tile] = []
    var fsBusyUntil = 0.0
    var noMaxTitles: Set<String> = []

    // -- Dock overlay ------------------------------------------------------------
    var dockItems: [Tile] = []
    var dockItemsAt = 0.0
    var desktopTile: Tile?
    var dockHover: String?
    var dockHoverSince = 0.0
    var dockLatched: String?
    var menuKind: String?
    var menuDock: Tile?
    var dockActions: [DockAction] = []
    var dockMarked: [String: String] = [:]

    // -- un-fullscreen scrims ----------------------------------------------------
    var prevThumbTitles: Set<String> = []
    var pendingUnfsBaseline: Set<String>?
    var pendingUnfsExpires = 0.0
    var pendingUnfsApp: String?
    var unfsPending: [String: String] = [:]

    // -- make-fullscreen / arrange / desktop -------------------------------------
    var fsMakeTitle: String?
    var fsMakeRect: (title: String, x: Double, y: Double)?
    var fsMakeOwner: String?
    var fsMakeWindow: AXUIElement?
    var fsMakeWaits = 0
    var fsMakeSettled = false
    var arrangeQueue: [(region: String, title: String)] = []
    var arrangeCurrent: (title: String, pids: [pid_t], pid: pid_t?, region: String)?
    var desktopWait = 0
    var desktopBefore: [Int: RoundedRect] = [:]

    // -- probable-exit latch -----------------------------------------------------
    var exitLatched = false
    var exitLatchSig: (spaces: [RoundedRect], thumbs: [RoundedRect])?
    var exitLatchExpires = 0.0

    // -- scene probe -------------------------------------------------------------
    var sceneProbe: NSPanel?
    var sceneProbeRect: Rect?
    var cornerProbes: [NSPanel] = []
    var cornerRects: [(panel: NSPanel, expect: Rect)] = []
    /// Initialised OPEN so headless tests exercise the downstream logic;
    /// activation arms it closed.
    var sceneStreak = Tuning.sceneStreakMin
    var sceneDeadTicks = 0
    var sceneClosedTicks = 0
    var probeDecim = 0

    // -- settle machine ------------------------------------------------------------
    var settledThumbsSig: [RoundedRect]?
    var candidateSig: [RoundedRect]?
    var candidateSince = 0.0
    var prevBarSettled = false
    var commitUntil = 0.0
    var cgSig: [WindowSig]?
    var prevCGSig: [WindowSig]?
    /// Last raw trackpad touch frame. Deliberately NOT reset per session.
    var lastTouch = 0.0

    // -- quiescent skip --------------------------------------------------------------
    var watchIDs: [Int] = []
    var watchSig: [WindowSig]?
    var pulsePrev: [WindowSig]?
    var pulsePrevT = 0.0
    var nearBarPrev: Bool?
    var barBurstUntil = 0.0
    var lastFullSync = 0.0
    var quietSkips = 0
    var quietFulls = 0
    var lastMouseY: Double?
    var hoverIndex: Int?
    var perfFrames: [(Double, Double)] = []

    // -- marks -----------------------------------------------------------------------
    /// {title: glyph} thumbnails marked for a deferred action.
    var marked: [String: String] = [:]

    // -- flyout menus --------------------------------------------------------------
    var menuOpen = false
    var menuTitle: String?
    var menuRects: [Int: Rect] = [:]
    var menuActions: [String] = []
    var menuPanel: NSPanel?
    var menuView: MenuView?
    var hoverQuitIndex: Int?
    var hoverQuitSince = 0.0
    var menuViaHover = false
    var menuAwaySince = 0.0
    var menuAnchorRect: Rect?
    var menuPanelRect: Rect?

    // -- diagnostics ------------------------------------------------------------------
    var diagCount = 0
    var diagSigs: Set<String> = []
    var loggedCaps: String?
    var loggedFS: String?
    var loggedPanelKeys: [PanelKey]?

    init(ax: AXBridge = .shared) {
        self.ax = ax
        super.init()
    }

    let ownPid = ProcessInfo.processInfo.processIdentifier

    // MARK: - lifecycle

    func start() {
        Log.prepareDebugLogs()
        startDetectTimer()
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(self, selector: #selector(spaceChanged(_:)),
                           name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)
        // Window sets only change around app activations and space switches,
        // so the registry is refreshed on those events.
        center.addObserver(self, selector: #selector(appActivated(_:)),
                           name: NSWorkspace.didActivateApplicationNotification, object: nil)
        // Doze the detect poll while the display sleeps.
        center.addObserver(self, selector: #selector(screensSlept(_:)),
                           name: NSWorkspace.screensDidSleepNotification, object: nil)
        center.addObserver(self, selector: #selector(screensWoke(_:)),
                           name: NSWorkspace.screensDidWakeNotification, object: nil)
        // The screens-wake notice isn't always posted after a full system
        // sleep, so any of these also wakes the poll.
        for name in [NSWorkspace.didWakeNotification, NSWorkspace.sessionDidBecomeActiveNotification] {
            center.addObserver(self, selector: #selector(screensWoke(_:)), name: name, object: nil)
        }
    }

    func stop() {
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        stopDetectTimer()
        if active { deactivate() }
    }

    /// Delayed self-messages go through here (tests override it).
    func schedule(_ selector: Selector, after delay: Double) {
        perform(selector, with: nil, afterDelay: delay)
    }

    func cancelScheduled(_ selector: Selector) {
        NSObject.cancelPreviousPerformRequests(withTarget: self, selector: selector, object: nil)
    }

    func startDetectTimer(_ interval: Double = Tuning.detectInterval) {
        if let timer = detectTimer, timer.isValid, abs(timer.timeInterval - interval) < 1e-6 { return }
        stopDetectTimer()
        let timer = Timer(timeInterval: interval, target: self, selector: #selector(tick(_:)),
                          userInfo: nil, repeats: true)
        // Let macOS coalesce the idle wake-ups with other system work.
        timer.tolerance = min(interval * 0.3, 1.0)
        RunLoop.main.add(timer, forMode: .common)
        detectTimer = timer
    }

    func stopDetectTimer() {
        detectTimer?.invalidate()
        detectTimer = nil
    }

    @objc func screensSlept(_ note: Notification?) {
        if active { deactivate() }
        // Doze rather than stop: a slow tick notices the displays are back
        // even if no wake notice ever arrives.
        dozing = true
        startDetectTimer(Tuning.dozeInterval)
    }

    @objc func screensWoke(_ note: Notification?) {
        dozing = false
        if !active { startDetectTimer() }
    }

    // MARK: - Mission Control detection

    @objc func tick(_ timer: Timer?) {
        if dozing {
            if CGDisplayIsAsleep(CGMainDisplayID()) != 0 { return }
            screensWoke(nil)  // awake again; no notice came
            return
        }
        doTick()
    }

    func doTick() {
        if active {
            // syncTick owns liveness while Mission Control is up; this slow
            // tick only makes sure it is still running.
            if syncTimer == nil || !(syncTimer?.isValid ?? false)
                || Clock.now() - lastSyncTick > Tuning.syncStall {
                Log.note("watchdog reset a stalled session")
                deactivate()
            }
            return
        }
        // Idle path: one WindowServer query; the AX tree is touched only when
        // Mission Control actually looks active.
        if let group = ax.pollMissionControl() {
            activate(group)
            return
        }
        if Clock.monotonic() - capsRefreshedAt >= 2.0 {
            capsRefreshedAt = Clock.monotonic()
            ax.refreshFrontWindowCaps()
        }
        if lastRegistryScan == 0 { scanRegistry() }  // first scan / post-MC rescan
    }

    func activate(_ group: AXUIElement) {
        active = true
        activatedAt = Clock.now()
        lastSyncTick = Clock.now()
        // The idle detect poll slows to a 1 Hz watchdog while MC is up.
        startDetectTimer(Tuning.watchdogInterval)
        mcGroup = group
        ax.pinned.removeAll()  // last session's pins have been applied
        Log.geom(String(format: "ACTIVATE %.3f host=%@ backdrop=%@",
                        Clock.now().truncatingRemainder(dividingBy: 1000),
                        ax.missionControlHost(group), ax.backdropMode))
        measureScreens()
        // Arm the scene gate closed and raise the invisible probe: nothing is
        // read or rendered until the probe proves the enter zoom has finished.
        sceneStreak = 0
        sceneDeadTicks = 0
        sceneClosedTicks = 0
        settledThumbsSig = nil
        candidateSig = nil
        candidateSince = 0
        prevBarSettled = false
        commitUntil = 0
        cgSig = nil
        prevCGSig = nil
        watchIDs = []
        watchSig = nil
        pulsePrev = nil
        pulsePrevT = 0
        nearBarPrev = nil
        barBurstUntil = 0
        lastFullSync = 0
        showSceneProbe()
        setTapEnabled(true)
        startHoverPolling()
        // Entry runs at the fast cadence until the first layout is accepted.
        syncInterval = 0
        setSyncInterval(Tuning.syncIntervalFast)
        sync(group)
    }

    func measureScreens() {
        // AX coordinates are relative to the primary screen's top-left.
        guard let primary = NSScreen.screens.first else { return }
        let h = Double(primary.frame.height)
        screenHeight = h
        screenWidth = Double(primary.frame.width)
        screenRects = NSScreen.screens.map { s in
            let f = s.frame
            return Rect(Double(f.origin.x), h - Double(f.origin.y + f.height), Double(f.width), Double(f.height))
        }
    }

    /// (Re)schedule the sync timer at `interval`, if it isn't already there.
    /// Headless callers record the value only.
    func setSyncInterval(_ interval: Double) {
        if syncInterval == interval { return }
        syncInterval = interval
        syncTimer?.invalidate()
        syncTimer = nil
        if active {
            let timer = Timer(timeInterval: interval, target: self, selector: #selector(syncTick(_:)),
                              userInfo: nil, repeats: true)
            timer.tolerance = interval * 0.1
            RunLoop.main.add(timer, forMode: .common)
            syncTimer = timer
        }
    }

    func deactivate() {
        Log.geom(String(format: "DEACTIVATE %.3f (mc group gone)",
                        Clock.now().truncatingRemainder(dividingBy: 1000)))
        active = false
        startDetectTimer()
        mcGroup = nil
        sceneStreak = 0
        sceneProbe?.orderOut(nil)
        cornerProbes.forEach { $0.orderOut(nil) }
        syncTimer?.invalidate()
        syncTimer = nil
        setTapEnabled(false)
        stopHoverPolling()
        // Instant, not faded: Mission Control is gone, so a fade would float
        // the buttons over whatever screen replaced it.
        hideAll(immediate: true)
        exitLatched = false
        exitLatchSig = nil
        prevLayout = nil
        lastRegistryScan = 0  // spaces likely changed; rescan soon
        recentlyClosed = [:]
        marked = [:]
        pendingUnfsBaseline = nil
        pendingUnfsApp = nil
        unfsPending = [:]
        if !deferredActions.isEmpty {
            // Window actions clicked during Mission Control land now, with it
            // gone, so they leave no ghost thumbnail.
            schedule(#selector(applyDeferredActions), after: 0.35)
        }
        if !deferredClose.isEmpty {
            schedule(#selector(processDeferredCloses), after: 0.9)
        }
        dockMarked = [:]
        if !dockActions.isEmpty {
            schedule(#selector(applyDockActions), after: 0.35)
        }
        dockItemsAt = 0
        desktopTile = nil
        if !deferredAppCloses.isEmpty {
            appCloseAttempts = 0
            schedule(#selector(applyDeferredAppCloses), after: 0.5)
        }
    }

    @objc func syncTick(_ timer: Timer?) {
        lastSyncTick = Clock.now()
        guard active else { return }
        // Quiescent settled state, decided BEFORE any full query: watch only
        // the windows that matter via a ~0.1ms by-id description. Unchanged
        // signature ⇒ the overview cannot differ ⇒ this frame costs nothing.
        let now = Clock.now()
        let mouseY = lastMouseY ?? 0  // unknown counts as "near the bar"
        let beat = mouseY <= Tuning.barRegionHeight ? Tuning.quietResyncBar : Tuning.quietResync
        if settledThumbsSig != nil, watchSig != nil, now >= barBurstUntil, now - lastFullSync < beat,
           !Motion.moved(watchSig, ax.windowsAliveSignature(watchIDs)) {
            quietSkips += 1
            return
        }
        if now >= barBurstUntil, settledThumbsSig != nil, syncInterval == Tuning.syncIntervalFast {
            setSyncInterval(Tuning.syncInterval)  // a bar burst just ended
        }
        quietFulls += 1
        if Log.debugEnabled && quietSkips + quietFulls >= 100 {
            Log.geom("QUIET \(quietSkips) skipped / \(quietFulls) full frames")
            quietSkips = 0
            quietFulls = 0
        }
        // Full frame: one WindowServer list pass, no Dock IPC.
        let pulse = ax.missionControlPulse(excludePid: ownPid)
        cgSig = pulse.sig
        if !pulse.present {
            mcGroup = nil
            if ax.missionControlGroup() == nil {
                deactivate()
            } else {
                // AX tree still up: MC is animating closed (the tree stays
                // frozen at the settled layout through the exit zoom).
                Log.geom("EXIT-HIDE backdrop gone (group still up)")
                hideAll(immediate: true)
            }
            return
        }
        // Mid-transition (the scene probe reads transformed): nothing to do.
        if sceneStreak < Tuning.sceneStreakMin { return }
        // Real windows in motion: the AX tree just mirrors them, so walking it
        // now is pure waste. Stop at the pulse until motion ends, except for a
        // post-action glide. "Moving" requires TWO FRESH consecutive samples.
        let tol = settledThumbsSig != nil ? Motion.tolerance : 0
        let fresh = now - pulsePrevT < 0.25
        let cgMoving = fresh && Motion.moved(pulsePrev, cgSig, tol: tol)
        let prevForDebug = pulsePrev
        pulsePrev = cgSig
        pulsePrevT = now
        if cgMoving {
            let fingersUp = now - lastTouch >= Tuning.touchIdle
            let gliding = settledThumbsSig != nil && fingersUp && !recentlyClosed.isEmpty
            if !gliding {
                if !rects.isEmpty {
                    Log.geom("MOTION-HIDE (cg moving, no glide) \(Motion.diff(prevForDebug, cgSig))")
                    hideAll(immediate: true)
                }
                watchSig = nil
                return
            }
        } else if settledThumbsSig == nil, candidateSig != nil, now - lastFullSync < 0.15 {
            // Frozen mid-gesture hold: acceptance is timer/fingers-driven.
            return
        }
        // Reuse the cached group: verifying it is one AX call.
        var group = mcGroup
        if group == nil || !ax.missionControlAlive(group) {
            group = ax.missionControlGroup()
            mcGroup = group
        }
        guard let liveGroup = group else {
            deactivate()
            return
        }
        lastFullSync = now
        sync(liveGroup)
        watchIDs = pulse.watch
        watchSig = ax.windowsAliveSignature(pulse.watch)
    }

    func sync(_ group: AXUIElement?) {
        if !Log.debugEnabled {
            let (targets, marks) = collectTargets(group)
            syncPanels(targets)
            syncMarks(marks)
            return
        }
        let t0 = Clock.now()
        let (targets, marks) = collectTargets(group)
        let t1 = Clock.now()
        syncPanels(targets)
        syncMarks(marks)
        perfFrames.append((t1 - t0, Clock.now() - t1))
        if perfFrames.count >= 50 {
            let collect = perfFrames.map { $0.0 }, panelCost = perfFrames.map { $0.1 }
            Log.geom(String(format: "PERF %d frames: collect avg=%.1fms max=%.1fms | panels avg=%.2fms max=%.2fms",
                            perfFrames.count, 1000 * collect.reduce(0, +) / Double(collect.count),
                            1000 * (collect.max() ?? 0), 1000 * panelCost.reduce(0, +) / Double(panelCost.count),
                            1000 * (panelCost.max() ?? 0)))
            perfFrames = []
        }
    }

    // MARK: - window registry

    @objc func spaceChanged(_ note: Notification?) {
        if active && Clock.now() - activatedAt > 1.2 {
            // Leaving Mission Control (a tile/thumbnail click, a swipe). The
            // grace period skips the space change macOS makes while OPENING
            // Mission Control from a fullscreen space.
            Log.geom("EXIT-HIDE space changed")
            latchExit()
        }
        scheduleRescan(0.6)
    }

    @objc func appActivated(_ note: Notification?) {
        scheduleRescan(0.8)  // debounced: ⌘-Tabbing ends in ONE scan
    }

    func scheduleRescan(_ delay: Double) {
        cancelScheduled(#selector(rescanRegistry))
        schedule(#selector(rescanRegistry), after: delay)
    }

    @objc func rescanRegistry() {
        if active { return }
        let wait = lastRegistryScan + Tuning.registryMinGap - Clock.now()
        if lastRegistryScan != 0 && wait > 0 {
            scheduleRescan(wait)  // run when the gap ends
            return
        }
        scanRegistry()
    }

    func scanRegistry() {
        let now = Clock.now()
        if lastRegistryScan != 0 && now - lastRegistryScan < Tuning.registryMinGap { return }
        lastRegistryScan = now
        let (fresh, visible, byPid) = ax.scanClosableWindows()
        let running = ax.runningAppNames()
        var merged: [String: FullscreenEntry] = [:]
        for (name, entry) in fullscreenRegistry {
            if !running.contains(name) { continue }  // app quit
            if visible.contains(name) && fresh[name] == nil { continue }  // no longer fullscreen
            merged[name] = entry
        }
        merged.merge(fresh) { _, new in new }
        fullscreenRegistry = merged
        windowRegistry = byPid
        runningNames = running
        guard Log.debugEnabled else { return }
        let caps = ax.capsDebug.map { "\(ax.appName(for: $0.key.pid) ?? String($0.key.pid))/\($0.key.title): \($0.value)" }
            .sorted().joined(separator: "; ")
        if caps != loggedCaps {
            loggedCaps = caps
            Log.geom("registry caps \(caps)")
        }
        let fs = merged.keys.sorted().joined(separator: ", ")
        if fs != loggedFS {
            loggedFS = fs
            Log.geom("registry fullscreen=[\(fs)]")
        }
    }
}
