import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

/// A window button captured while Mission Control was closed (apps hide their
/// AX windows while it is open). Element references stay pressable later.
struct HeldWindow {
    var title: String
    var close: AXUIElement?
    var minimize: AXUIElement?
}

struct FullscreenEntry {
    var window: AXUIElement
    var close: AXUIElement
    var title: String
}

/// A retained, unambiguous window identity for a queued action.
struct CapturedWindow {
    var pid: pid_t
    var window: AXUIElement
    var number: CGWindowID?
}

struct CapKey: Hashable {
    var pid: pid_t
    var title: String
}

struct WindowCaps: Equatable {
    var first: Bool
    var second: Bool
    static let unknown = WindowCaps(first: true, second: true)
}

/// Reads Mission Control's AX tree (the Dock through macOS 26, WindowManager on
/// macOS 27) and acts on other apps' windows through the Accessibility API.
/// A port of ax.py; module-level state lives on the shared instance. Not
/// final: the tests subclass it to stand in for the live system.
class AXBridge {
    static let shared = AXBridge()

    static let dockBundle = "com.apple.dock"
    static let windowManagerBundle = "com.apple.WindowManager"

    // -- state ---------------------------------------------------------------

    private var pidCache: [String: pid_t] = [:]
    let backdrop: BackdropRule
    /// On-screen window ids from the latest pulse, used to drop thumbnails of
    /// windows on OTHER spaces (see currentSpaceOnly).
    private var onscreenIDs: Set<Int> = []
    private var onscreenAt = 0.0
    /// WindowManager's application element (macOS 27), whose children are the
    /// mc.display groups. Kept to tell it apart from the Dock's 'mc' group.
    private(set) var wmRoot: AXUIElement?
    private var wmRootPid: pid_t?
    private var preferWindowManager = AX.isMacOS27
    /// An mc.display element from the latest snapshot.
    private var lastDisplay: AXUIElement?
    /// Set once CGSGetActiveSpace's id has matched a thumbnail's '.space.<n>'.
    private var spaceIDsVerified = false
    private var spaceTypes: [Int: Int] = [:]
    /// {window title: bundle id} for every thumbnail seen (macOS 27).
    var fullscreenBundles: [String: String] = [:]

    /// {(pid, title): (can_fullscreen, can_maximize)}.
    var windowCaps: [CapKey: WindowCaps] = [:]
    /// {(pid, title): (can_close, can_minimize)}.
    var windowActionCaps: [CapKey: WindowCaps] = [:]
    var windowRefs: [pid_t: [(title: String, window: AXUIElement, number: CGWindowID?)]] = [:]
    /// Same-titled thumbnails keyed "<title><keySep><n>", pinned to a real
    /// window by geometry: key -> (pid, CG window number).
    var pinned: [String: (pid: pid_t, number: CGWindowID)] = [:]
    var capsDebug: [CapKey: String] = [:]

    init(backdrop: BackdropRule = BackdropRule()) {
        self.backdrop = backdrop
    }

    // -- permissions ---------------------------------------------------------

    /// Prompt for Accessibility access if missing. EMC deliberately does NOT
    /// ask for Screen Recording: thumbnails are matched by bundle id and AX.
    func ensurePermissions() -> Bool { AX.isTrusted(prompt: true) }

    // -- processes -----------------------------------------------------------

    private func bundlePid(_ bundle: String) -> pid_t? {
        if let pid = pidCache[bundle] { return pid }
        for app in NSWorkspace.shared.runningApplications where app.bundleIdentifier == bundle {
            pidCache[bundle] = app.processIdentifier
            return app.processIdentifier
        }
        return nil
    }

    private func forgetPid(_ bundle: String) { pidCache[bundle] = nil }

    func pidForBundle(_ bundle: String) -> pid_t? {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first?.processIdentifier
    }

    func appNameForBundle(_ bundle: String) -> String? {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
            .compactMap { $0.localizedName }.first { !$0.isEmpty }
    }

    func appName(for pid: pid_t) -> String? {
        NSRunningApplication(processIdentifier: pid)?.localizedName
    }

    func pidsNamed(_ name: String) -> [pid_t] {
        NSWorkspace.shared.runningApplications
            .filter { $0.localizedName == name }.map { $0.processIdentifier }
    }

    /// Localized names of all regular (Dock-visible) running apps.
    func runningAppNames() -> Set<String> {
        Set(NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular }
            .compactMap { $0.localizedName }.filter { !$0.isEmpty })
    }

    // -- Mission Control detection ---------------------------------------------

    func listWindows(excludePid: pid_t? = nil, allSpaces: Bool = false) -> [WindowInfo] {
        Windows.list(excludePid: excludePid, allSpaces: allSpaces)
    }

    var backdropMode: String { backdrop.mode.rawValue }

    /// Whether Mission Control's backdrop looks up (debug tracing only).
    func missionControlProbablyActive() -> Bool {
        backdrop.present(ownerLayerCounts(WindowList.onScreen()))
    }

    /// The idle detection poll: the Mission Control tree root when Mission
    /// Control is up, else nil. Costs one WindowServer query; the Dock /
    /// WindowManager are only asked when the backdrop rule says it's worth it.
    func pollMissionControl() -> AXUIElement? {
        let counts = ownerLayerCounts(WindowList.onScreen())
        guard backdrop.idleShouldCheck(counts, now: Clock.now()) else { return nil }
        let group = missionControlGroup()
        if group == nil {
            backdrop.idleCheckFoundNothing()
        } else {
            backdrop.confirmed(counts)
        }
        return group
    }

    /// System processes whose layer-0 windows are never app windows.
    static let pulseSkipOwners: Set<String> = [
        "Dock", "WindowManager", "Window Server", "Control Center", "Control Centre",
        "Notification Center", "Notification Centre", "Spotlight", "Wallpaper",
        "screencapture", "TextInputMenuAgent", "loginwindow",
    ]

    /// (present, realWindowSig, watchIDs) from ONE WindowServer query — the
    /// sync tick's per-frame heartbeat. `present` is the backdrop rule;
    /// `realWindowSig` is a rounded-bounds signature of the layer-0 app
    /// windows (excluding ours): at a gesture COMMIT the AX tree snaps to the
    /// final layout while these windows visibly ease after it.
    func missionControlPulse(excludePid: pid_t) -> (present: Bool, sig: [WindowSig], watch: [Int]) {
        var sig: [WindowSig] = []
        var watch: [Int] = []
        var onscreen: [Int] = []
        var band: [OwnerLayer: Int] = [:]
        for entry in WindowList.onScreen() {
            if entry.layer == 0 {
                if entry.pid == excludePid { continue }
                onscreen.append(entry.number)
                // Only real app windows: macOS 27 adds short-lived system
                // windows while you hover in Mission Control.
                if Self.pulseSkipOwners.contains(entry.owner) || entry.width < 80 || entry.height < 80 {
                    continue
                }
                sig.append(entry.sig)
                watch.append(entry.number)
            } else if (18...20).contains(entry.layer), missionControlWindowOwners.contains(entry.owner) {
                watch.append(entry.number)
                band[OwnerLayer(owner: entry.owner, layer: entry.layer), default: 0] += 1
            }
        }
        onscreenIDs = Set(onscreen + watch)
        onscreenAt = Clock.now()
        return (backdrop.present(band), sig, watch)
    }

    /// Rounded-bounds signature of SPECIFIC windows by id — the settled-state
    /// watchdog. A window that closed simply drops out, changing the signature.
    func windowsAliveSignature(_ ids: [Int]) -> [WindowSig] {
        WindowList.describe(ids).filter { $0.bounds != nil }.map { $0.sig }
    }

    private func hasDisplay(_ root: AXUIElement) -> Bool {
        AX.children(root).contains { AX.string($0, "AXIdentifier") == "mc.display" }
    }

    private func dockMCGroup() -> AXUIElement? {
        guard let pid = bundlePid(Self.dockBundle) else { return nil }
        let dock = AX.app(pid)
        guard let children = AX.attribute(dock, "AXChildren") as? [AnyObject] else {
            forgetPid(Self.dockBundle)
            return nil
        }
        for child in children.compactMap({ AX.element($0) })
        where AX.string(child, "AXIdentifier") == "mc" {
            // macOS 27 leaves an empty 'mc' stub here; only a populated group
            // is the real tree.
            return AX.children(child).isEmpty ? nil : child
        }
        return nil
    }

    private func windowManagerRoot() -> AXUIElement? {
        guard let pid = bundlePid(Self.windowManagerBundle) else { return nil }
        if wmRoot == nil || wmRootPid != pid {
            wmRoot = AX.app(pid)
            wmRootPid = pid
        }
        guard let root = wmRoot else { return nil }
        if AX.attribute(root, "AXChildren") == nil {
            forgetPid(Self.windowManagerBundle)
            return nil
        }
        return hasDisplay(root) ? root : nil
    }

    /// The element whose children are Mission Control's mc.display groups —
    /// present only while Mission Control is up.
    func missionControlGroup() -> AXUIElement? {
        let order: [Bool] = preferWindowManager ? [true, false] : [false, true]
        for useWM in order {
            if let group = useWM ? windowManagerRoot() : dockMCGroup() {
                preferWindowManager = useWM  // ask the host that answered first next time
                return group
            }
        }
        return nil
    }

    func isWindowManagerRoot(_ group: AXUIElement?) -> Bool {
        guard let group = group, let root = wmRoot else { return false }
        return group === root
    }

    func missionControlHost(_ group: AXUIElement?) -> String {
        isWindowManagerRoot(group) ? "WindowManager" : "Dock"
    }

    /// True while `group` still shows Mission Control. WindowManager's app
    /// element never dies, so for it this means "still has an mc.display".
    func missionControlAlive(_ group: AXUIElement?) -> Bool {
        guard let group = group else { return false }
        if isWindowManagerRoot(group) {
            let children = AX.children(group)
            if let last = lastDisplay, children.contains(where: { CFEqual($0, last) }) {
                return true
            }
            return children.contains { AX.string($0, "AXIdentifier") == "mc.display" }
        }
        return AX.alive(group)
    }

    // -- the Mission Control snapshot -------------------------------------------

    private func displays(_ group: AXUIElement) -> [AXUIElement] {
        AX.children(group).filter { AX.string($0, "AXIdentifier") == "mc.display" }
    }

    /// Spaces Bar tiles report their CENTER as AXPosition when WindowManager
    /// hosts the tree (macOS 27); the Dock reported the top-left corner.
    private func spaceOrigin(_ group: AXUIElement, _ p: CGPoint, _ s: CGSize) -> CGPoint {
        isWindowManagerRoot(group) ? CGPoint(x: p.x - s.width / 2, y: p.y - s.height / 2) : p
    }

    // macOS 26's Dock reported a Spaces Bar tile as just its preview image;
    // macOS 27's WindowManager reports the whole tile (padding and label
    // included). Convert back to the preview so buttons don't cover macOS's
    // own remove/close (X) button on it.
    static let previewWidthRatio = 138.0 / 168.0
    static let previewTopShare = 0.44

    private func spacePreviewRect(_ group: AXUIElement, _ x: Double, _ y: Double,
                                  _ width: Double, _ height: Double) -> Rect {
        guard isWindowManagerRoot(group), width > 0 else { return Rect(x, y, width, height) }
        var aspect = 956.0 / 1470.0
        let screens = NSScreen.screens
        if let primary = screens.first {
            let primaryH = Double(primary.frame.height)
            let cx = x + width / 2, cy = y + height / 2
            var chosen = primary
            for screen in screens {
                let f = screen.frame
                let top = primaryH - Double(f.origin.y + f.height)
                if Double(f.origin.x) <= cx, cx <= Double(f.origin.x + f.width),
                   top <= cy, cy <= top + Double(f.height) {
                    chosen = screen
                    break
                }
            }
            if chosen.frame.width > 0 {
                aspect = Double(chosen.frame.height / chosen.frame.width)
            }
        }
        let pw = width * Self.previewWidthRatio
        let ph = min(height, pw * aspect)
        return Rect(x + (width - pw) / 2, y + (height - ph) * Self.previewTopShare, pw, ph)
    }

    static let thumbAttrs = ["AXTitle", "AXPosition", "AXSize", "AXDescription", "AXIdentifier"]
    static let spaceAttrs = ["AXTitle", "AXDescription", "AXPosition", "AXSize"]
    static let childAttrs = ["AXIdentifier", "AXRole", "wid"] + thumbAttrs

    /// One pass over an mc.display: ([(thumb element, attrs)], [space tiles]).
    /// macOS 26 nests thumbnails in an mc.windows group; macOS 27 puts them
    /// directly under mc.display (identifier '<bundle id>.space.<n>').
    private func displayParts(_ display: AXUIElement)
        -> (buttons: [(AXUIElement, [String: AnyObject])], tiles: [AXUIElement]) {
        var direct: [(AXUIElement, [String: AnyObject])] = []
        var nested: [(AXUIElement, [String: AnyObject])]?
        var tiles: [AXUIElement] = []
        for child in AX.children(display) {
            let vals = AX.multi(child, Self.childAttrs)
            let ident = vals["AXIdentifier"] as? String
            if ident == "mc.windows" {
                nested = AX.children(child).map { ($0, AX.multi($0, Self.thumbAttrs)) }
            } else if ident == "mc.spaces" {
                for part in AX.children(child) where AX.string(part, "AXIdentifier") == "mc.spaces.list" {
                    tiles = AX.children(part)
                }
            } else if !(ident ?? "").hasPrefix("mc."), vals["AXRole"] as? String == "AXButton" {
                direct.append((child, vals))
            }
        }
        return (nested ?? direct, tiles)
    }

    private func windowID(_ vals: [String: AnyObject]) -> Int? {
        (vals["wid"] as? NSNumber)?.intValue
    }

    /// macOS 27 lists the DESKTOP's thumbnails even while Mission Control is
    /// showing a fullscreen space. Its thumbnails carry their CGWindowID
    /// ('wid'), and only the viewed space's windows are on screen, so keep the
    /// thumbnails whose window is on screen — only when the latest window list
    /// is fresh and holds at least one thumbnail's window.
    private func currentSpaceOnly(_ buttons: [(AXUIElement, [String: AnyObject])])
        -> [(AXUIElement, [String: AnyObject])] {
        if Clock.now() - onscreenAt > 0.5 { return buttons }
        let ids = buttons.map { windowID($0.1) }
        guard ids.contains(where: { $0 != nil && onscreenIDs.contains($0!) }) else { return buttons }
        return zip(buttons, ids).filter { $0.1 == nil || onscreenIDs.contains($0.1!) }.map { $0.0 }
    }

    /// 'com.apple.Safari.space.3' -> ('com.apple.Safari', 3).
    static func splitThumbID(_ ident: String?) -> (bundle: String?, space: Int?) {
        guard let ident = ident, let range = ident.range(of: ".space.", options: .backwards) else {
            return (nil, nil)
        }
        let bundle = String(ident[..<range.lowerBound])
        return (bundle, Int(ident[range.upperBound...]))
    }

    /// The CURRENT space id of every display. CGSGetActiveSpace only answers
    /// for the display with the menu-bar focus, so on its own it filtered out
    /// every window on a second display. Falls back to it when unavailable.
    private func activeSpaceIDs() -> Set<Int> {
        spaceTypes = [:]
        var ids = Set<Int>()
        for display in PrivateAPI.managedDisplaySpaces() ?? [] {
            for space in display["Spaces"] as? [[String: Any]] ?? [] {
                let sid = (space["ManagedSpaceID"] as? NSNumber) ?? (space["id64"] as? NSNumber)
                if let sid = sid, let type = space["type"] as? NSNumber {
                    spaceTypes[sid.intValue] = type.intValue
                }
            }
            if let current = display["Current Space"] as? [String: Any] {
                let sid = (current["ManagedSpaceID"] as? NSNumber) ?? (current["id64"] as? NSNumber)
                if let sid = sid, sid.intValue != 0 { ids.insert(sid.intValue) }
            }
        }
        if ids.isEmpty, let active = PrivateAPI.activeSpaceID() { ids.insert(active) }
        return ids
    }

    /// Identify full-screen previews by Space, not by app or preview size.
    private func fullscreenThumbnail(_ thumb: Tile, _ spaces: [Tile]) -> Bool {
        if let space = thumb.space, let kind = spaceTypes[space] {
            return kind == 4  // native full-screen Space (including Split View)
        }
        return !thumb.title.isEmpty && spaces.contains {
            $0.title == thumb.title && $0.desc.hasPrefix("exit to full screen")
        }
    }

    /// macOS 27 lists the windows of EVERY full-screen space among the current
    /// desktop's thumbnails — stacked, invisible, at one centred frame (the
    /// "ghost window"). Keep only the shown space's thumbnails.
    private func dropOtherSpaceThumbs(_ thumbs: [Tile], _ spaces: [Tile]) -> [Tile] {
        let numbered = thumbs.filter { $0.space != nil }
        if numbered.isEmpty { return thumbs }
        let active = activeSpaceIDs()
        if !active.isEmpty, numbered.contains(where: { active.contains($0.space!) }) {
            spaceIDsVerified = true
            return thumbs.filter { $0.space == nil || active.contains($0.space!) }
        }
        if !active.isEmpty && spaceIDsVerified {
            // The ids have matched real thumbnails before: no match now means
            // an EMPTY desktop, whose overview lists other desktops' windows.
            return thumbs.filter { $0.space == nil }
        }
        let fsTitles = Set(spaces.filter { $0.desc.hasPrefix("exit to full screen") }.map { $0.title })
        var bySpace: [Int: [Tile]] = [:]
        for t in numbered { bySpace[t.space!, default: []].append(t) }
        let fsSpaces = Set(bySpace.filter { _, members in
            members.allSatisfy { fsTitles.contains($0.title) } && bySpace.count > 1
        }.keys)
        return thumbs.filter { $0.space == nil || !fsSpaces.contains($0.space!) }
    }

    /// Record {title: bundle} for every thumbnail BEFORE any space filter, so
    /// a full-screen tile's title can be traced to its app.
    private func noteBundles(_ buttons: [(AXUIElement, [String: AnyObject])]) {
        for (_, vals) in buttons {
            let bundle = Self.splitThumbID(vals["AXIdentifier"] as? String).bundle
            if let bundle = bundle, let title = vals["AXTitle"] as? String,
               !title.trimmingCharacters(in: .whitespaces).isEmpty {
                fullscreenBundles[title] = bundle
            }
        }
    }

    /// One walk of the Mission Control subtree returning (thumbnails, spaces,
    /// windowCount) — everything a sync frame needs. Each element's
    /// attributes come from one batched AX call.
    func missionControlSnapshot(_ group: AXUIElement?) -> (thumbs: [Tile], spaces: [Tile], windowCount: Int) {
        var thumbs: [Tile] = []
        var spaces: [Tile] = []
        var windowCount = 0
        guard let group = group else { return (thumbs, spaces, windowCount) }
        for display in displays(group) {
            lastDisplay = display
            let parts = displayParts(display)
            var buttons = parts.buttons
            let tiles = parts.tiles
            noteBundles(buttons)
            buttons = currentSpaceOnly(buttons)
            windowCount += buttons.count
            for (button, vals) in buttons {
                guard let p = AX.point(vals["AXPosition"]), let s = AX.size(vals["AXSize"]) else { continue }
                var title = vals["AXTitle"] as? String ?? ""
                if title.trimmingCharacters(in: .whitespaces).isEmpty {
                    title = vals["AXDescription"] as? String ?? ""
                }
                let (bundle, space) = Self.splitThumbID(vals["AXIdentifier"] as? String)
                if title.trimmingCharacters(in: .whitespaces).isEmpty, let bundle = bundle {
                    // An untitled window of a real app (Safari's Start Page):
                    // name it by its app; the lookups resolve an app-name
                    // title to that app's lone untitled window.
                    title = appNameForBundle(bundle) ?? ""
                }
                thumbs.append(Tile(title: title, x: Double(p.x), y: Double(p.y),
                                   width: Double(s.width), height: Double(s.height),
                                   element: button, bundle: bundle, space: space))
            }
            for button in tiles {
                let vals = AX.multi(button, Self.spaceAttrs)
                guard let p0 = AX.point(vals["AXPosition"]), let s = AX.size(vals["AXSize"]) else { continue }
                let p = spaceOrigin(group, p0, s)
                let r = spacePreviewRect(group, Double(p.x), Double(p.y), Double(s.width), Double(s.height))
                spaces.append(Tile(title: vals["AXTitle"] as? String ?? "",
                                   desc: vals["AXDescription"] as? String ?? "",
                                   x: r.x, y: r.y, width: r.w, height: r.h, element: button))
            }
        }
        if isWindowManagerRoot(group) {
            let before = thumbs.count
            thumbs = dropOtherSpaceThumbs(thumbs, spaces)
            windowCount -= before - thumbs.count
            for i in thumbs.indices {
                thumbs[i].fullscreen = fullscreenThumbnail(thumbs[i], spaces)
            }
        }
        return (thumbs, spaces, windowCount)
    }

    /// Structural dump of the Mission Control subtree (debug diagnostics).
    func describeMCTree(_ group: AXUIElement?, maxDepth: Int = 4) -> [String] {
        var lines: [String] = []
        func walk(_ element: AXUIElement, _ depth: Int) {
            let role = AX.string(element, "AXRole") ?? "-"
            let ident = AX.string(element, "AXIdentifier") ?? "-"
            let sub = AX.string(element, "AXSubrole") ?? "-"
            let title = AX.string(element, "AXTitle") ?? ""
            let desc = AX.string(element, "AXDescription") ?? ""
            let children = AX.children(element)
            var frame = ""
            if let f = AX.frame(of: element) {
                frame = String(format: "@(%.0f,%.0f %.0fx%.0f)", f.x, f.y, f.w, f.h)
            }
            let pad = String(repeating: "  ", count: depth)
            lines.append("    \(pad)\(role) id=\(ident) sub=\(sub) title='\(title)' desc='\(desc)' \(frame) children=\(children.count)")
            if depth < maxDepth { children.forEach { walk($0, depth + 1) } }
        }
        if let group = group { walk(group, 0) }
        return lines
    }

    // -- title / pid resolution ---------------------------------------------------

    static let keySep = "\u{2063}#"   // invisible separator + '#'

    /// The window title behind a thumbnail key.
    static func baseTitle(_ title: String) -> String {
        guard let range = title.range(of: keySep) else { return title }
        return String(title[..<range.lowerBound])
    }

    /// True when two window titles refer to the same window despite the Dock
    /// showing a shortened form for the thumbnail: exact, or one a prefix of
    /// the other. Empty never relates to anything.
    static func titlesRelated(_ a: String, _ b: String) -> Bool {
        if a == b { return true }
        if a.isEmpty || b.isEmpty { return false }
        return a.hasPrefix(b) || b.hasPrefix(a)
    }

    /// Best-effort pid behind a thumbnail/tile title: the thumbnail's bundle
    /// id, else an app with that name.
    func pidForTitle(_ title: String) -> pid_t? {
        let title = Self.baseTitle(title)
        var bundle = fullscreenBundles[title]
        if bundle == nil {
            bundle = fullscreenBundles.first { Self.titlesRelated(title, $0.key) }?.value
        }
        if let bundle = bundle, let pid = pidForBundle(bundle) { return pid }
        return pidsNamed(title).first
    }

    func focusedWindow(of pid: pid_t) -> AXUIElement? {
        let app = AX.app(pid)
        return AX.child(app, "AXFocusedWindow") ?? AX.child(app, "AXMainWindow")
    }

    func findFullscreenWindow(_ appName: String?) -> AXUIElement? {
        guard let appName = appName else { return nil }
        for pid in pidsNamed(appName) {
            if let w = AX.windows(of: pid).first(where: { AX.bool($0, "AXFullScreen") == true }) {
                return w
            }
        }
        return nil
    }

    /// A fullscreen window whose AX *window title* matches (the Dock retitles
    /// a fullscreen space by its window title after a split partner closes).
    func findFullscreenWindowByTitle(_ windowTitle: String) -> AXUIElement? {
        guard !windowTitle.isEmpty else { return nil }
        for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
            for w in AX.windows(of: app.processIdentifier)
            where AX.bool(w, "AXFullScreen") == true && (AX.string(w, "AXTitle") ?? "") == windowTitle {
                return w
            }
        }
        return nil
    }

    /// A Spaces Bar tile title to its fullscreen window: app name, then title.
    func findFullscreenTarget(_ tileTitle: String) -> AXUIElement? {
        findFullscreenWindow(tileTitle) ?? findFullscreenWindowByTitle(tileTitle)
    }

    // -- window actions -------------------------------------------------------

    /// Raise the window, make it main, and bring its app frontmost.
    func focusWindow(_ window: AXUIElement) {
        AX.set(window, "AXMain", kCFBooleanTrue)
        AX.perform(window, "AXRaise")
        if let pid = AX.pid(of: window) { activatePid(pid) }
    }

    @discardableResult
    func activatePid(_ pid: pid_t) -> Bool {
        guard let app = NSRunningApplication(processIdentifier: pid) else { return false }
        app.activate(options: [.activateIgnoringOtherApps])
        return true
    }

    /// ⌘Q — the app may prompt to save.
    func quitPid(_ pid: pid_t) -> Bool {
        NSRunningApplication(processIdentifier: pid)?.terminate() ?? false
    }

    /// Force Quit (no prompt).
    func forceQuitPid(_ pid: pid_t) -> Bool {
        NSRunningApplication(processIdentifier: pid)?.forceTerminate() ?? false
    }

    /// Raw SIGKILL to the exact pid (works for helper processes too).
    func killPid(_ pid: pid_t) -> Bool { kill(pid, SIGKILL) == 0 }

    /// Press the close button of every window the app reports.
    func closeAllWindows(_ pid: pid_t) -> Int {
        AX.windows(of: pid).reduce(0) { n, w in AX.press(AX.child(w, "AXCloseButton")) ? n + 1 : n }
    }

    // -- Show Desktop ----------------------------------------------------------

    /// The user's Show Desktop shortcut (symbolic hot key 36, F11 by default)
    /// as (keycode, flags), or nil when it's turned off.
    func showDesktopHotkey() -> (keycode: CGKeyCode, flags: UInt64)? {
        let keys = CFPreferencesCopyAppValue("AppleSymbolicHotKeys" as CFString,
                                             "com.apple.symbolichotkeys" as CFString) as? [String: Any]
        guard let entry = keys?["36"] as? [String: Any] else {
            return (103, 0x800000)  // never customised: F11 (fn)
        }
        if let enabled = entry["enabled"] as? NSNumber, !enabled.boolValue { return nil }
        guard let params = (entry["value"] as? [String: Any])?["parameters"] as? [NSNumber],
              params.count >= 3 else { return nil }
        return (CGKeyCode(params[1].intValue), params[2].uint64Value)
    }

    /// {window number: frame} of on-screen app windows (layer 0).
    func appWindowFrames() -> [Int: RoundedRect] {
        var frames: [Int: RoundedRect] = [:]
        for w in WindowList.onScreen() where w.layer == 0 {
            let s = w.sig
            frames[w.number] = RoundedRect(x: s.x, y: s.y, w: s.w, h: s.h)
        }
        return frames
    }

    /// Toggle Show Desktop: "dock" posts the Dock notification (falls back to
    /// the shortcut); "key" presses the user's Show Desktop shortcut.
    func showDesktop(method: String) -> Bool {
        if method == "dock", PrivateAPI.coreDockNotify("com.apple.showdesktop.awake") {
            return true
        }
        guard let hotkey = showDesktopHotkey() else { return false }
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: nil, virtualKey: hotkey.keycode, keyDown: down)
            event?.flags = CGEventFlags(rawValue: hotkey.flags)
            event?.post(tap: .cghidEventTap)
        }
        return true
    }

    // -- registry / capabilities --------------------------------------------------

    /// One AX sweep over all regular running apps, while Mission Control is
    /// CLOSED (apps stop reporting their AX windows while it is open). The
    /// returned element references stay pressable during Mission Control.
    func scanClosableWindows()
        -> (fullscreen: [String: FullscreenEntry], visible: Set<String>, byPid: [pid_t: [HeldWindow]]) {
        var fullscreen: [String: FullscreenEntry] = [:]
        var visible = Set<String>()
        var byPid: [pid_t: [HeldWindow]] = [:]
        for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
            guard let name = app.localizedName, !name.isEmpty else { continue }
            let pid = app.processIdentifier
            let windows = AX.windows(of: pid)
            if !windows.isEmpty { visible.insert(name) }
            rememberWindows(pid, windows)
            if !windows.isEmpty {
                // Re-read this app's windows from scratch (drop closed ones).
                for key in windowCaps.keys where key.pid == pid { windowCaps[key] = nil }
                for key in capsDebug.keys where key.pid == pid { capsDebug[key] = nil }
                for key in windowActionCaps.keys where key.pid == pid { windowActionCaps[key] = nil }
            }
            var entries: [HeldWindow] = []
            for window in windows {
                let close = AX.child(window, "AXCloseButton")
                let title = AX.string(window, "AXTitle") ?? ""
                noteCaps(pid, title, window)
                guard let closeButton = close else { continue }
                entries.append(HeldWindow(title: title, close: closeButton,
                                          minimize: AX.child(window, "AXMinimizeButton")))
                if fullscreen[name] == nil, AX.bool(window, "AXFullScreen") == true {
                    fullscreen[name] = FullscreenEntry(window: window, close: closeButton, title: title)
                }
            }
            if !entries.isEmpty { byPid[pid] = entries }
        }
        return (fullscreen, visible, byPid)
    }

    private func rememberWindows(_ pid: pid_t, _ windows: [AXUIElement]) {
        guard !windows.isEmpty else { return }
        windowRefs[pid] = windows.map {
            (title: AX.string($0, "AXTitle") ?? "", window: $0, number: PrivateAPI.windowNumber(of: $0))
        }
    }

    /// Bound idle refresh to the foreground app; never poll all apps per frame.
    func refreshFrontWindowCaps() {
        guard let app = NSWorkspace.shared.frontmostApplication else { return }
        let pid = app.processIdentifier
        let windows = AX.windows(of: pid)
        rememberWindows(pid, windows)
        guard !windows.isEmpty else { return }
        for key in windowCaps.keys where key.pid == pid { windowCaps[key] = nil }
        for key in windowActionCaps.keys where key.pid == pid { windowActionCaps[key] = nil }
        for w in windows { noteCaps(pid, AX.string(w, "AXTitle") ?? "", w) }
    }

    private func noteCaps(_ pid: pid_t, _ title: String, _ window: AXUIElement) {
        let key = CapKey(pid: pid, title: title)
        let canClose = AX.child(window, "AXCloseButton").map { AX.bool($0, "AXEnabled") != false } ?? false
        let canMin = AX.child(window, "AXMinimizeButton").map { AX.bool($0, "AXEnabled") != false } ?? false
        windowActionCaps[key] = WindowCaps(first: canClose, second: canMin)
        let fsButton = AX.child(window, "AXFullScreenButton")
        var canFS = true
        if let fsButton = fsButton {
            if AX.bool(fsButton, "AXEnabled") == false { canFS = false }
        } else {
            // No full-screen button at all (System Settings on macOS 27): only
            // full-screen-able if its AXFullScreen attribute is settable.
            canFS = AX.settable(window, "AXFullScreen") ?? false
        }
        let resizable = AX.settable(window, "AXSize") ?? true
        windowCaps[key] = WindowCaps(first: canFS, second: resizable && canFS)
        if Log.debugEnabled {
            capsDebug[key] = "fsbtn=\(fsButton != nil) enabled=\(String(describing: AX.bool(fsButton, "AXEnabled"))) resizable=\(resizable)"
        }
    }

    /// (can_fullscreen, can_maximize) for a thumbnail, or unknown → (true,
    /// true): never hide a button on a guess.
    func capsFor(_ pid: pid_t?, _ title: String) -> WindowCaps {
        cachedCaps(windowCaps, pid, title)
    }

    /// (can_close, can_minimize); unknown windows keep the legacy fallbacks.
    func actionCapsFor(_ pid: pid_t?, _ title: String) -> WindowCaps {
        cachedCaps(windowActionCaps, pid, title)
    }

    private func cachedCaps(_ cache: [CapKey: WindowCaps], _ pid: pid_t?, _ title: String) -> WindowCaps {
        guard let pid = pid else { return .unknown }
        let title = Self.baseTitle(title)
        if let caps = cache[CapKey(pid: pid, title: title)] { return caps }
        let related = cache.filter { $0.key.pid == pid && Self.titlesRelated(title, $0.key.title) }
        if related.count == 1 { return related.first!.value }
        // Apps whose AX window title is blank (System Settings) can't be
        // matched by title; if the app has just one known window, it's that.
        let mine = cache.filter { $0.key.pid == pid }
        return mine.count == 1 ? mine.first!.value : .unknown
    }

    // -- retained identities ----------------------------------------------------

    func windowNumber(_ window: AXUIElement?) -> CGWindowID? {
        guard let window = window else { return nil }
        return PrivateAPI.windowNumber(of: window)
    }

    var windowNumbersAvailable: Bool { PrivateAPI.windowNumbersAvailable }

    /// Retain an unambiguous window identity while its preview is selected.
    func captureWindow(_ title: String) -> CapturedWindow? {
        let pin = pinned[title]
        guard let pid = pin?.pid ?? pidForTitle(title) else { return nil }
        let entries = windowRefs[pid] ?? []
        var hits: [(title: String, window: AXUIElement, number: CGWindowID?)]
        if let pin = pin {
            hits = entries.filter { $0.number == pin.number }
        } else if title.contains(Self.keySep) {
            return nil  // never reduce an unresolved twin to a plain title
        } else {
            let base = Self.baseTitle(title)
            hits = entries.filter { $0.title == base }
            if hits.isEmpty { hits = entries.filter { Self.titlesRelated(base, $0.title) } }
            if hits.isEmpty, entries.count == 1, entries[0].title.isEmpty { hits = entries }
        }
        guard hits.count == 1 else { return nil }
        return CapturedWindow(pid: pid, window: hits[0].window, number: hits[0].number)
    }

    /// Act on the retained window, never a replacement with the same title.
    func performCapturedAction(_ target: CapturedWindow, _ action: String) -> Bool {
        if let number = target.number, windowNumber(target.window) != number { return false }
        let attr = action == "close" ? "AXCloseButton" : "AXMinimizeButton"
        guard let button = buttonOf(target.window, attr), AX.bool(button, "AXEnabled") != false else {
            return false
        }
        foregroundWindow(target.window)
        return pressElement(button)
    }

    /// true = observed completion, false = still pending, nil = unreadable.
    /// Never infer a close from an empty AXWindows list: off-Space apps hide it.
    func capturedActionState(_ target: CapturedWindow, _ action: String) -> Bool? {
        let attr = action == "minimize" ? "AXMinimized" : "AXRole"
        let (err, value) = copyAttribute(target.window, attr)
        if action == "close" && err == .invalidUIElement { return true }
        guard err == .success else { return nil }
        if action == "minimize" {
            if let value = value, CFGetTypeID(value) == CFBooleanGetTypeID() {
                return CFBooleanGetValue((value as! CFBoolean))
            }
            return (value as? NSNumber)?.boolValue ?? false
        }
        return false
    }

    // Test seams over the raw AX calls used by the identity paths.
    func copyAttribute(_ element: AXUIElement, _ name: String) -> (AXError, AnyObject?) {
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(element, name as CFString, &value)
        return (err, value)
    }

    func buttonOf(_ window: AXUIElement?, _ attr: String) -> AXUIElement? {
        AX.child(window, attr)
    }

    func pressElement(_ element: AXUIElement?) -> Bool { AX.press(element) }

    // -- Dock icons ---------------------------------------------------------------

    /// Running apps' Dock icons (top-left coords). The Dock still hosts its
    /// icons on macOS 27 (only Mission Control's tree moved).
    func dockAppItems() -> [Tile] {
        guard let pid = bundlePid(Self.dockBundle) else { return [] }
        var items: [Tile] = []
        for child in AX.children(AX.app(pid)) where AX.string(child, "AXRole") == "AXList" {
            for item in AX.children(child) {
                let vals = AX.multi(item, ["AXSubrole", "AXTitle", "AXPosition", "AXSize",
                                           "AXIsApplicationRunning", "AXURL"])
                guard vals["AXSubrole"] as? String == "AXApplicationDockItem" else { continue }
                guard (vals["AXIsApplicationRunning"] as? NSNumber)?.boolValue == true else { continue }
                guard let p = AX.point(vals["AXPosition"]), let s = AX.size(vals["AXSize"]) else { continue }
                var bundle: String?
                if let url = vals["AXURL"] as? URL { bundle = Bundle(url: url)?.bundleIdentifier }
                items.append(Tile(title: vals["AXTitle"] as? String ?? "", x: Double(p.x), y: Double(p.y),
                                  width: Double(s.width), height: Double(s.height), bundle: bundle))
            }
        }
        return items
    }

    /// AXRemoveDesktop on a Spaces Bar tile. For a fullscreen app's tile the
    /// Dock exits fullscreen — Mission Control stays open.
    func removeSpace(_ element: AXUIElement) -> Bool { AX.perform(element, "AXRemoveDesktop") }

    // -- fullscreen helpers ---------------------------------------------------

    /// Re-find a window of the app after its element may have been recreated.
    func findAppWindow(_ appName: String, held: AXUIElement?, title: String?) -> AXUIElement? {
        var fallback: AXUIElement?
        for pid in pidsNamed(appName) {
            for w in AX.windows(of: pid) {
                if AX.same(w, held) { return w }
                if let title = title, fallback == nil, AX.string(w, "AXTitle") == title { fallback = w }
            }
        }
        return fallback
    }

    @discardableResult
    func setFullscreen(_ window: AXUIElement, _ flag: Bool) -> Bool {
        AX.set(window, "AXFullScreen", flag ? kCFBooleanTrue : kCFBooleanFalse) == .success
    }

    /// Put the app's main/focused window into full screen via its AX
    /// full-screen button, else the AXFullScreen attribute.
    func fullscreenWindowOf(_ appName: String) -> Bool {
        for pid in pidsNamed(appName) {
            let app = AX.app(pid)
            guard let window = AX.child(app, "AXMainWindow") ?? AX.child(app, "AXFocusedWindow") else {
                continue
            }
            if let button = AX.child(window, "AXFullScreenButton") { return AX.press(button) }
            return setFullscreen(window, true)
        }
        return false
    }

    /// Close the app's main/focused window.
    func closeMainWindow(_ appName: String) -> Bool {
        for pid in pidsNamed(appName) {
            let app = AX.app(pid)
            guard let window = AX.child(app, "AXMainWindow") ?? AX.child(app, "AXFocusedWindow") else {
                continue
            }
            if AX.press(AX.child(window, "AXCloseButton")) { return true }
        }
        return false
    }

    func closeFullscreenWindow(_ tileTitle: String) -> Bool {
        guard let window = findFullscreenTarget(tileTitle) else { return false }
        return AX.press(AX.child(window, "AXCloseButton"))
    }

    // -- same-titled windows ---------------------------------------------------

    /// The live AX window a thumbnail key was pinned to.
    func pinnedWindow(_ key: String) -> AXUIElement? {
        guard let entry = pinned[key] else { return nil }
        return AX.windows(of: entry.pid).first { windowNumber($0) == entry.number }
    }

    /// [(window number, rect)] for the app's on-screen windows.
    func appWindowRects(_ pid: pid_t) -> [(number: Int, rect: Rect)] {
        WindowList.onScreen().compactMap { e in
            guard e.pid == pid, e.layer == 0, let b = e.bounds, b.width >= 80, b.height >= 80 else { return nil }
            return (number: e.number, rect: Rect(Double(b.origin.x), Double(b.origin.y), Double(b.width), Double(b.height)))
        }
    }

    /// Each rect's centre normalised to the group's bounding box, plus its
    /// log aspect ratio — comparable between thumbnails and real windows.
    private static func layoutPoints(_ rects: [Rect]) -> [(Double, Double, Double)] {
        let x0 = rects.map { $0.x }.min() ?? 0
        let y0 = rects.map { $0.y }.min() ?? 0
        let w = max((rects.map { $0.x + $0.w }.max() ?? 0) - x0, 1)
        let h = max((rects.map { $0.y + $0.h }.max() ?? 0) - y0, 1)
        return rects.map { r in
            ((r.x + r.w / 2 - x0) / w, (r.y + r.h / 2 - y0) / h, log(max(r.w, 1) / max(r.h, 1)))
        }
    }

    /// Pair thumbnails with windows (equal counts): the assignment that best
    /// keeps their relative layout and shapes. Returns a window index per
    /// thumbnail.
    static func matchLayout(_ thumbRects: [Rect], _ windowRects: [Rect]) -> [Int] {
        let a = layoutPoints(thumbRects), b = layoutPoints(windowRects)
        let cost = a.map { p in b.map { q in abs(p.0 - q.0) + abs(p.1 - q.1) + 0.5 * abs(p.2 - q.2) } }
        let n = a.count
        if n <= 7 {
            var best: [Int] = Array(0..<n)
            var bestCost = Double.infinity
            var perm = Array(0..<n)
            func permute(_ k: Int) {
                if k == n {
                    let c = (0..<n).reduce(0.0) { $0 + cost[$1][perm[$1]] }
                    if c < bestCost { bestCost = c; best = perm }
                    return
                }
                for i in k..<n {
                    perm.swapAt(k, i)
                    permute(k + 1)
                    perm.swapAt(k, i)
                }
            }
            permute(0)
            return best
        }
        var order: [Int] = []
        var used = Set<Int>()
        for i in 0..<n {
            let j = (0..<n).filter { !used.contains($0) }.min { cost[i][$0] < cost[i][$1] }!
            used.insert(j)
            order.append(j)
        }
        return order
    }

    // -- title-matched window actions -----------------------------------------

    func closeWindowByTitle(_ title: String, _ pids: [pid_t], foreground: Bool = false) -> Bool {
        pressWindowButtonByTitle(title, pids, "AXCloseButton", foreground)
    }

    func minimizeWindowByTitle(_ title: String, _ pids: [pid_t], foreground: Bool = false) -> Bool {
        pressWindowButtonByTitle(title, pids, "AXMinimizeButton", foreground)
    }

    /// Press a window's close/minimize button, matching the thumbnail title to
    /// the live window: exact first, else a UNIQUE prefix match. Ambiguous
    /// matches are ignored so the wrong window is never touched.
    private func pressWindowButtonByTitle(_ title: String, _ pids: [pid_t], _ attr: String,
                                          _ foreground: Bool) -> Bool {
        guard !title.isEmpty else { return false }
        if foreground {
            // Resolve once so raising and pressing refer to the same window.
            guard let window = findWindowByTitle(title, pids),
                  let target = buttonOf(window, attr) else { return false }
            foregroundWindow(window)
            return pressElement(target)
        }
        if pinned[title] != nil {
            return pressElement(buttonOf(pinnedWindow(title), attr))
        }
        let base = Self.baseTitle(title)
        var exact: [AXUIElement] = [], related: [AXUIElement] = []
        for pid in pids {
            for window in AX.windows(of: pid) {
                guard let button = AX.child(window, attr) else { continue }
                let wt = AX.string(window, "AXTitle") ?? ""
                if wt == base { exact.append(button) } else if Self.titlesRelated(base, wt) { related.append(button) }
            }
        }
        var target: AXUIElement? = exact.count == 1 ? exact[0]
            : (exact.isEmpty && related.count == 1 ? related[0] : nil)
        if target == nil { target = buttonOf(untitledWindowOf(base, pids), attr) }
        if target == nil { target = buttonOf(ownerWindowFor(base, attr), attr) }
        return pressElement(target)
    }

    /// Last-resort match through the thumbnail's own app: the app's only
    /// window, else the single window whose title contains (or is contained
    /// in) the thumbnail's, case-insensitively.
    private func ownerWindowFor(_ title: String, _ attr: String? = nil) -> AXUIElement? {
        guard let pid = pidForTitle(title) else { return nil }
        let windows = AX.windows(of: pid).filter { attr == nil || AX.child($0, attr!) != nil }
        if windows.count == 1 { return windows[0] }
        let wanted = title.trimmingCharacters(in: .whitespaces).lowercased()
        guard !wanted.isEmpty else { return nil }
        let hits = windows.filter {
            let have = (AX.string($0, "AXTitle") ?? "").trimmingCharacters(in: .whitespaces).lowercased()
            return !have.isEmpty && (have.contains(wanted) || wanted.contains(have))
        }
        return hits.count == 1 ? hits[0] : nil
    }

    /// The live window matching a thumbnail title among `pids`.
    func findWindowByTitle(_ title: String, _ pids: [pid_t]) -> AXUIElement? {
        guard !title.isEmpty else { return nil }
        if pinned[title] != nil { return pinnedWindow(title) }
        let base = Self.baseTitle(title)
        var exact: [AXUIElement] = [], related: [AXUIElement] = []
        for pid in pids {
            for window in AX.windows(of: pid) {
                let wt = AX.string(window, "AXTitle") ?? ""
                if wt == base { exact.append(window) } else if Self.titlesRelated(base, wt) { related.append(window) }
            }
        }
        if !exact.isEmpty { return exact.count == 1 ? exact[0] : nil }
        if related.count == 1 { return related[0] }
        return untitledWindowOf(base, pids) ?? ownerWindowFor(base)
    }

    /// An untitled thumbnail is titled by its app's name: resolve it to that
    /// app's ONLY untitled window among `pids`.
    private func untitledWindowOf(_ appName: String, _ pids: [pid_t]) -> AXUIElement? {
        guard !appName.isEmpty else { return nil }
        let wanted = Set(pids)
        var found: [AXUIElement] = []
        for pid in pidsNamed(appName) where wanted.isEmpty || wanted.contains(pid) {
            found += AX.windows(of: pid).filter {
                (AX.string($0, "AXTitle") ?? "").trimmingCharacters(in: .whitespaces).isEmpty
            }
        }
        return found.count == 1 ? found[0] : nil
    }

    /// The AX frame for a snap region of the screen the window sits on, using
    /// that screen's *visible* area (clear of the menu bar and the Dock).
    func snapFrame(_ region: String, _ win: Rect) -> Rect? {
        let screens = NSScreen.screens
        guard let primary = screens.first else { return nil }
        let primaryH = Double(primary.frame.height)
        func toAX(_ r: NSRect) -> Rect {
            Rect(Double(r.origin.x), primaryH - Double(r.origin.y + r.height), Double(r.width), Double(r.height))
        }
        let cx = win.x + win.w / 2, cy = win.y + win.h / 2
        let chosen = screens.first { toAX($0.frame).contains(cx, cy) } ?? primary
        let v = toAX(chosen.visibleFrame)
        switch region {
        case "left": return Rect(v.x, v.y, v.w / 2, v.h)
        case "right": return Rect(v.x + v.w / 2, v.y, v.w / 2, v.h)
        default: return v  // 'max' — whole visible area (not macOS full screen)
        }
    }

    /// Move+resize the window matching a thumbnail title to a region of its
    /// screen's visible area. Best-effort.
    func snapWindowByTitle(_ title: String, _ pids: [pid_t], _ region: String) -> Bool {
        guard let window = findWindowByTitle(title, pids), let frame = AX.frame(of: window),
              let target = snapFrame(region, frame) else { return false }
        var point = CGPoint(x: target.x, y: target.y)
        var size = CGSize(width: target.w, height: target.h)
        guard let pos = AXValueCreate(.cgPoint, &point), let sz = AXValueCreate(.cgSize, &size) else {
            return false
        }
        // Position, size, position again: one can be constrained by the other.
        let r1 = AX.set(window, "AXPosition", pos)
        AX.set(window, "AXSize", sz)
        let r2 = AX.set(window, "AXPosition", pos)
        return r1 == .success || r2 == .success
    }

    // Native macOS tiling (macOS 15+): Window ▸ Move & Resize ▸ Left/Right/Fill.
    static let tileWindowMenu = ["Window"]
    static let tileSubmenu = ["Move & Resize"]
    static let tileLeaf: [String: [String]] = ["left": ["Left"], "right": ["Right"], "max": ["Fill", "Maximize"]]

    private func submenuItems(_ element: AXUIElement) -> [AXUIElement] {
        for child in AX.children(element) where AX.string(child, "AXRole") == "AXMenu" {
            return AX.children(child)
        }
        return []
    }

    private func menuItem(_ items: [AXUIElement], _ titles: [String]) -> AXUIElement? {
        items.first { titles.contains(AX.string($0, "AXTitle") ?? "") }
    }

    /// Make the window matching a thumbnail title main, raise it, and bring
    /// its app frontmost. Returns the owning pid.
    func focusWindowByTitle(_ title: String, _ pids: [pid_t]) -> pid_t? {
        guard let window = findWindowByTitle(title, pids) else { return nil }
        return foregroundWindow(window)
    }

    /// Request foreground ordering without adding an animation delay.
    @discardableResult
    func foregroundWindow(_ window: AXUIElement) -> pid_t? {
        guard let pid = AX.pid(of: window) else { return nil }
        AX.set(window, "AXMain", kCFBooleanTrue)
        AX.perform(window, "AXRaise")
        activatePid(pid)
        return pid
    }

    /// Press the native tiling menu leaf for the app's focused window.
    func tileFocusedApp(_ pid: pid_t, _ region: String) -> Bool {
        guard let leafTitles = Self.tileLeaf[region], pid != 0 else { return false }
        let app = AX.app(pid)
        guard let menuBar = AX.child(app, "AXMenuBar"),
              let windowMenu = menuItem(AX.children(menuBar), Self.tileWindowMenu) else { return false }
        let items = submenuItems(windowMenu)
        var candidates: [AXUIElement] = []
        if let submenu = menuItem(items, Self.tileSubmenu) { candidates += submenuItems(submenu) }
        candidates += items
        // Don't pre-check AXEnabled: right after activation the item may still
        // read disabled; AXPress fails on a truly disabled item.
        return AX.press(menuItem(candidates, leafTitles))
    }
}
