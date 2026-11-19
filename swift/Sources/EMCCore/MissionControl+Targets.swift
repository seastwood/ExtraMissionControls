import AppKit
import ApplicationServices
import Foundation

typealias Target = (kind: String, tile: Tile)
typealias Mark = (tile: Tile, glyph: String)

extension MissionControlController {
    // MARK: - targets

    func collectTargets(_ group: AXUIElement?) -> ([Target], [Mark]) {
        // Scene gate: while Mission Control's enter/exit zoom runs, nothing on
        // screen is where AX says it is and nothing is clickable.
        if sceneStreak < Tuning.sceneStreakMin {
            traceGate("scene-transforming", 0, [], [])
            return ([], [])
        }
        let now = Clock.now()
        recentlyClosed = recentlyClosed.filter { $0.value > now }
        // One batched walk for everything this frame needs.
        let snapshot = ax.missionControlSnapshot(group)
        // Full-screen previews belong to macOS's native Space controls.
        var thumbs = snapshot.thumbs.filter { !$0.fullscreen }
        let spaces = snapshot.spaces
        let windowCount = snapshot.windowCount
        keyDuplicates(&thumbs)
        logGeometry(group, thumbs, spaces)
        lastThumbs = thumbs
        writeDiag(group, thumbs, spaces)

        // A full-screen app closed from the Spaces Bar reappears as a desktop
        // thumbnail with an unpredictable title: mark whichever title shows up
        // that wasn't present when the ✕ was clicked.
        let currentTitles = Set(thumbs.map { $0.title }.filter { !$0.isEmpty })
        if let baseline = pendingUnfsBaseline {
            if now < pendingUnfsExpires {
                for title in currentTitles.subtracting(baseline) where marked[title] == nil {
                    marked[title] = "✕"
                    if let app = pendingUnfsApp { unfsPending[title] = app }
                    Log.geom("UNFS-SCRIM: '\(title)' reappeared -> marked")
                }
            } else {
                pendingUnfsBaseline = nil
                pendingUnfsApp = nil
            }
        }
        prevThumbTitles = currentTitles

        // Track the Spaces Bar and the thumbnails for motion SEPARATELY: a
        // button is only placed once its own layer holds still.
        let spacesSig = spaces.map { $0.rounded }
        let thumbsSig = thumbs.map { $0.rounded }
        let prev = prevLayout
        prevLayout = (spacesSig, thumbsSig)
        guard let previous = prev else {
            traceGate("baseline", windowCount, spaces, thumbs)
            return ([], [])  // first frame after (re)activation: baseline
        }
        let spacesStill = spacesSig == previous.spaces
        let thumbsStill = thumbsSig == previous.thumbs

        // Probable-exit latch: hidden while the layout still matches its
        // latch-time signature, released by movement or by expiry.
        if exitLatched {
            if exitLatchSig == nil { exitLatchSig = (spacesSig, thumbsSig) }
            if let sig = exitLatchSig, sig.spaces == spacesSig, sig.thumbs == thumbsSig,
               now < exitLatchExpires {
                traceGate("exit-latched", windowCount, spaces, thumbs)
                return ([], [])
            }
            exitLatched = false
            exitLatchSig = nil
            traceGate("exit-latch released", windowCount, spaces, thumbs)
        }

        // Settle confirmation: a thumbs layout is trusted when the bar just
        // turned settled (the gesture commit), or after holding still.
        let barOK = barSettled(spaces)
        if barOK && !prevBarSettled && settledThumbsSig == nil {
            // The thumbnails jump in the SAME frame as the bar snaps, so open
            // a short window and accept the first still layout inside it.
            commitUntil = now + 0.5
        }
        prevBarSettled = barOK
        // Cursor-independent commit signal: AX still, real windows easing.
        let cgMoving = prevCGSig != nil && cgSig != prevCGSig
        prevCGSig = cgSig
        if cgMoving && thumbsStill && settledThumbsSig == nil {
            Log.geom("COMMIT-EASE detected (AX still, windows easing)")
            commitUntil = now + 0.5
        }
        let fingersUp = now - lastTouch >= Tuning.touchIdle
        if thumbsStill && thumbsSig != settledThumbsSig {
            if candidateSig != thumbsSig {
                candidateSig = thumbsSig
                candidateSince = now
            }
            let confirm = barOK ? Tuning.settleConfirm : Tuning.settleConfirmNoBar
            if fingersUp || now < commitUntil || now - candidateSince >= confirm {
                settledThumbsSig = thumbsSig
                candidateSig = nil
                commitUntil = 0
                setSyncInterval(Tuning.syncInterval)  // entry is over
            }
        } else if !thumbsStill {
            candidateSig = nil
        }
        let mcSettled = thumbsStill && thumbsSig == settledThumbsSig
        // Post-action re-flow glide: keep emitting at the live AX coords so the
        // panels MOVE with their thumbnails rather than blinking.
        let gliding = !mcSettled && !thumbsStill && settledThumbsSig != nil
            && fingersUp && !recentlyClosed.isEmpty
        if !mcSettled && !gliding {
            traceGate("await-settle bar_ok=\(barOK ? 1 : 0) still=\(thumbsStill ? 1 : 0)",
                      windowCount, spaces, thumbs)
            return ([], [])
        }
        if gliding { traceGate("gliding re-flow", windowCount, spaces, thumbs) }

        // A window grown to nearly fill the screen means MC is zooming into or
        // out of a full-screen space: suppress every button until it ends.
        if thumbs.contains(where: zooming) {
            traceGate("zoom-suppress", windowCount, spaces, thumbs)
            return ([], [])
        }
        // No on-screen thumbnails but a positive count: a fullscreen/split
        // space is being viewed, whose subtree reports stale coordinates.
        let onScreen = thumbs.filter(onScreenTile)
        if onScreen.isEmpty && windowCount > 0 {
            traceGate("fs-suppress", windowCount, spaces, thumbs)
            return ([], [])
        }

        var targets: [Target] = []
        var marks: [Mark] = []
        noMaxTitles = []
        if thumbsStill || gliding {
            for thumb in onScreen {
                // A titleless thumbnail is a tooltip/popup MC caught mid-hover;
                // every action is keyed by title, so no buttons.
                if untitled(thumb) { continue }
                if let glyph = markGlyph(for: thumb.title) {
                    marks.append((thumb, glyph))
                    // The pending action can be taken back: float ↺ over it.
                    if deferredAction(for: thumb.title) != nil || unfsPending[thumb.title] != nil
                        || dockMarked[thumb.title] != nil {
                        targets.append(("cancel", slot(thumb, 0, "↺", UI.hoverBlue)))
                    }
                    continue
                }
                let pid = thumb.bundle.flatMap { ax.pidForBundle($0) }
                let caps = ax.capsFor(pid, thumb.title)
                if !caps.second { noMaxTitles.insert(thumb.title) }
                let actionCaps = ax.actionCapsFor(pid, thumb.title)
                var n = 0
                if actionCaps.first {
                    targets.append(("window", slot(thumb, n)))
                    n += 1
                }
                if actionCaps.second {
                    targets.append(("minimize", slot(thumb, n, "−", UI.hoverYellow)))
                    n += 1
                }
                if caps.first {  // hidden for windows that can't go full screen
                    targets.append(("makefullscreen", slot(thumb, n, "⤢", UI.hoverGreen)))
                    n += 1
                }
                targets.append(("arrange", slot(thumb, n, "◫", UI.hoverTeal)))
                n += 1
                targets.append(("quit", slot(thumb, n, "⏻", UI.hoverPurple)))
            }
            // Show Desktop, alone in the bottom-right corner above the Dock.
            if let desktop = desktopTileFor() { targets.append(("desktop", desktop)) }
        }

        // Dock icons with a pending Quit / Force Quit: dim the icon and float
        // ↺ just ABOVE it.
        for action in dockActions {
            guard let icon = dockItems.first(where: { $0.title == action.name }) else { continue }
            marks.append((icon, Tuning.dockGlyphs[action.action] ?? "⏻"))
            let size = min(Double(UI.closeButtonSize), max(Tuning.minButtonSize, icon.height * 0.45))
            var badge = icon
            badge.x = icon.x + icon.width / 2 - size / 2 - Tuning.buttonInset
            badge.y = icon.y - size - 6 - Tuning.buttonInset
            targets.append(("cancel", slot(badge, 0, "↺", UI.hoverBlue)))
        }

        // macOS 27: no Spaces Bar buttons at all (see Tuning.barButtons).
        if !Tuning.barButtons { return (targets, marks) }
        let settled = settledSpaces(spaces)
        if !(spacesStill && thumbsStill && !settled.isEmpty) {
            traceGate("bar-gated emit W=\(targets.count) sp_still=\(spacesStill ? 1 : 0) th_still=\(thumbsStill ? 1 : 0)",
                      windowCount, spaces, thumbs)
            return (targets, marks)
        }
        for space in settled where onScreenTile(space) {
            if recentlyClosed["acted|\(space.title)"] != nil { continue }
            if let glyph = markGlyph(for: space.title) {
                marks.append((space, glyph))
                // A held-reference full-screen close still sits in the bar.
                if deferredClose.contains(where: { $0.title == space.title }) {
                    targets.append(("cancel", slot(space, 0, "↺", UI.hoverBlue)))
                }
                continue
            }
            if let apps = splitApps(space) {
                // Split View: one button closes BOTH, labeled ✕2.
                targets.append(("splitall", slot(space, 0, "✕\(apps.count)")))
            } else if isFullscreenTile(space) {
                targets.append(("fullscreen", slot(space, 0)))
            } else {
                continue
            }
            targets.append(("unfullscreen", slot(space, 1, "❏", UI.hoverGreen)))
        }
        traceGate("full emit n=\(targets.count)", windowCount, spaces, thumbs)
        return (targets, marks)
    }

    /// The dim-scrim glyph for a title: exact, else a UNIQUE loose relation.
    func markGlyph(for title: String) -> String? {
        if let glyph = marked[title] { return glyph }
        let related = marked.filter { looselySame(title, $0.key) }.map { $0.value }
        return related.count == 1 ? related[0] : nil
    }

    /// The pending action queued for this thumbnail, matched like markGlyph.
    func deferredAction(for title: String) -> DeferredAction? {
        if let exact = deferredActions.first(where: { $0.title == title }) { return exact }
        let related = deferredActions.filter { looselySame(title, $0.title) }
        return related.count == 1 ? related[0] : nil
    }

    /// A thumbnail inherits a mark made under a related title only when no
    /// thumbnail carries the marked title exactly.
    func looselySame(_ title: String, _ markedTitle: String) -> Bool {
        guard AXBridge.titlesRelated(title, markedTitle) else { return false }
        return !lastThumbs.contains { $0.title == markedTitle }
    }

    /// A per-button copy of a tile placed in a top-left slot.
    func slot(_ tile: Tile, _ n: Int, _ label: String? = nil, _ hover: RGB? = nil) -> Tile {
        var out = tile
        out.slot = n
        if let label = label { out.label = label }
        if let hover = hover { out.hover = hover }
        return out
    }

    func barSettled(_ spaces: [Tile]) -> Bool { !settledSpaces(spaces).isEmpty }

    /// The Spaces Bar tiles on displays whose bar is expanded at the top of
    /// its screen. Each display has its own bar.
    func settledSpaces(_ spaces: [Tile]) -> [Tile] {
        var groups: [Rect?: [Tile]] = [:]
        var order: [Rect?] = []
        for space in spaces {
            let screen = screenFor(space)
            if groups[screen] == nil { order.append(screen) }
            groups[screen, default: []].append(space)
        }
        var settled: [Tile] = []
        for screen in order {
            let members = groups[screen] ?? []
            let top = screen?.y ?? 0
            if (members.map { $0.height }.min() ?? 0) >= Tuning.minBarTileHeight,
               (members.map { $0.y }.min() ?? 0) >= top {
                settled += members
            }
        }
        return settled
    }

    /// The Show Desktop button's tile: always bottom-right of the primary
    /// display, a Dock's height up (left of a right-side Dock).
    func desktopTileFor() -> Tile? {
        if let tile = desktopTile { return tile }
        let sw = screenWidth ?? 0, sh = screenHeight ?? 0
        var tilesize = 48.0
        var orientation = "bottom"
        if let value = CFPreferencesCopyAppValue("tilesize" as CFString, "com.apple.dock" as CFString) as? NSNumber {
            tilesize = value.doubleValue
        }
        if let value = CFPreferencesCopyAppValue("orientation" as CFString, "com.apple.dock" as CFString) as? String {
            orientation = value
        }
        let band = dockBand(tilesize, orientation, sw, sh)
        var right = sw, bottom = sh
        if orientation == "right" {
            right -= band
        } else if orientation != "left" {
            bottom -= band
        }
        let size = Double(UI.closeButtonSize)
        let tile = Tile(title: "Show Desktop",
                        x: right - Tuning.desktopMargin - size - Tuning.buttonInset,
                        y: bottom - Tuning.desktopMargin - size - Tuning.buttonInset,
                        width: size + 2 * Tuning.buttonInset, height: 60,
                        slot: 0,
                        // a screen with arrows pushing outward
                        label: "sf:rectangle.arrowtriangle.2.outward@90|▭", hover: UI.hoverBlue)
        desktopTile = tile
        return tile
    }

    /// How far the Dock reaches in from its edge: the largest measured for
    /// this icon size, remembered across launches; an estimate until seen.
    func dockBand(_ tilesize: Double, _ orientation: String, _ sw: Double, _ sh: Double) -> Double {
        let defaults = UserDefaults.standard
        let key = "EMCDockBand.\(orientation).\(Int(tilesize))"
        var known = defaults.double(forKey: key)
        if dockItemsAt == 0 {
            dockItemsAt = Clock.now()
            dockItems = ax.dockAppItems()
        }
        let items = dockItems.filter { 0 <= $0.x && $0.x <= sw && 0 <= $0.y && $0.y <= sh }
        var measured = 0.0
        if !items.isEmpty {
            if orientation == "right" {
                measured = sw - (items.map { $0.x }.min() ?? sw) + Tuning.dockPad
            } else if orientation != "left" {
                measured = sh - (items.map { $0.y }.min() ?? sh) + Tuning.dockPad
            }
        }
        if tilesize * 0.8 <= measured && measured <= tilesize * 3 && measured > known {
            known = measured
            defaults.set(known, forKey: key)
        }
        return known > 0 ? known : tilesize * 1.3 + 2 * Tuning.dockPad
    }

    /// The AX rect of the display holding the tile's centre, or nil.
    func screenFor(_ tile: Tile) -> Rect? {
        let cx = tile.x + tile.width / 2, cy = tile.y + tile.height / 2
        return screenRects.first { $0.contains(cx, cy) }
    }

    /// True when the tile's centre is on a display (thumbnails of spaces
    /// panned away from get no buttons).
    func onScreenTile(_ tile: Tile) -> Bool {
        if !screenRects.isEmpty { return screenFor(tile) != nil }
        let w = screenWidth ?? 0, h = screenHeight ?? 0
        let cx = tile.x + tile.width / 2, cy = tile.y + tile.height / 2
        return 0 <= cx && cx <= w && 0 <= cy && cy <= h
    }

    /// True when a thumbnail is nearly screen-sized — the transient zoom as
    /// MC opens from, or closes into, a full-screen window.
    func zooming(_ tile: Tile) -> Bool {
        var w = screenWidth ?? 0, h = screenHeight ?? 0
        if !screenRects.isEmpty, let screen = screenFor(tile) {
            w = screen.w
            h = screen.h
        }
        guard w > 0, h > 0 else { return false }
        return tile.width >= 0.85 * w && tile.height >= 0.85 * h
    }

    /// No (or whitespace) title: a tooltip/popup, not a manageable window.
    func untitled(_ tile: Tile) -> Bool {
        tile.title.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// Split View tile: title 'A & B', desc 'exit to full screen A & B'.
    func splitApps(_ space: Tile) -> [String]? {
        guard space.title.contains(" & "), space.desc.contains("full screen") else { return nil }
        let parts = space.title.components(separatedBy: " & ").map { $0.trimmingCharacters(in: .whitespaces) }
        // Guard against an app literally named "Foo & Bar".
        return parts.allSatisfy { !$0.isEmpty && (runningNames.contains($0) || fullscreenRegistry[$0] != nil) }
            ? parts : nil
    }

    /// True for Spaces Bar tiles backed by a fullscreen app.
    func isFullscreenTile(_ space: Tile) -> Bool {
        if space.title.isEmpty { return false }
        if fullscreenRegistry[space.title] != nil { return true }
        if space.desc.contains("full screen") { return true }
        return runningNames.contains(space.title)
    }

    /// Thumbnails sharing a title get distinct keys "<title><sep><n>",
    /// numbered in reading order, so marks and actions stay on the one clicked.
    func keyDuplicates(_ thumbs: inout [Tile]) {
        var groups: [String: [Int]] = [:]
        for (i, t) in thumbs.enumerated() where !t.title.isEmpty { groups[t.title, default: []].append(i) }
        for (title, members) in groups where members.count >= 2 {
            let sorted = members.sorted { a, b in
                let ta = thumbs[a], tb = thumbs[b]
                let ra = ((ta.y + ta.height / 2) / 60).rounded(), rb = ((tb.y + tb.height / 2) / 60).rounded()
                if ra != rb { return ra < rb }
                return ta.x + ta.width / 2 < tb.x + tb.width / 2
            }
            for (n, index) in sorted.enumerated() {
                thumbs[index].title = "\(title)\(AXBridge.keySep)\(n + 1)"
            }
        }
    }

    // MARK: - diagnostics (EMC_DEBUG only)

    func traceGate(_ decision: String, _ wc: Int, _ spaces: [Tile], _ thumbs: [Tile]) {
        guard Log.debugEnabled else { return }
        let pa = ax.missionControlProbablyActive() ? 1 : 0
        let bar = spaces.first.map { String(format: "bar(y=%.0f h=%.0f n=%d)", $0.y, $0.height, spaces.count) }
            ?? "bar(none)"
        let big = thumbs.max { $0.width * $0.height < $1.width * $1.height }
        let thumb = big.map { String(format: "thumb(max %.0fx%.0f@%.0f,%.0f n=%d)", $0.width, $0.height, $0.x, $0.y,
                                     thumbs.count) } ?? "thumb(none)"
        Log.geom(String(format: "GATE %.3f wc=%d pa=%d ", Clock.now().truncatingRemainder(dividingBy: 1000), wc, pa)
                 + "\(bar) \(thumb) -> \(decision)")
    }

    func logGeometry(_ group: AXUIElement?, _ thumbs: [Tile], _ spaces: [Tile]) {
        guard Log.debugEnabled else { return }
        let sig = (spaces.map { "\($0.title)\($0.rounded)" } + ["|"] + thumbs.map { "\($0.title)\($0.rounded)" })
            .joined(separator: ";")
        if sig == geomSignature { return }
        geomSignature = sig
        var lines = [String(format: "---- layout (screen %.0fx%.0f) ----", screenWidth ?? 0, screenHeight ?? 0)]
        for s in spaces {
            lines.append(String(format: "  space  %-22@ @(%.0f,%.0f  %.0fx%.0f)", "'\(s.title)'", s.x, s.y,
                                s.width, s.height))
        }
        for t in thumbs {
            lines.append(String(format: "  window %-22@ @(%.0f,%.0f  %.0fx%.0f)", "'\(t.title)'", t.x, t.y,
                                t.width, t.height))
        }
        if thumbs.isEmpty {
            lines.append("  [mc tree]")
            lines += ax.describeMCTree(group)
        }
        Log.debug(lines.joined(separator: "\n"))
    }

    /// macOS 27: dump the first few distinct layouts of each launch (tree,
    /// thumbnails, on-screen windows) to emc_diag.txt. At most 4 writes.
    func writeDiag(_ group: AXUIElement?, _ thumbs: [Tile], _ spaces: [Tile]) {
        guard Log.debugEnabled, ax.isWindowManagerRoot(group), diagCount < 4 else { return }
        let sig = thumbs.map { "\($0.title)@\(Int($0.x.rounded())),\(Int($0.y.rounded()))" }.joined(separator: ";")
        if diagSigs.contains(sig) { return }
        diagSigs.insert(sig)
        diagCount += 1
        var lines = ["==== layout \(diagCount) @ \(Date()) ====", "[thumbs]"]
        lines += thumbs.map { String(format: "  '%@' @(%.0f,%.0f %.0fx%.0f)", $0.title, $0.x, $0.y, $0.width, $0.height) }
        lines.append("[spaces]")
        lines += spaces.map {
            String(format: "  '%@' desc='%@' @(%.0f,%.0f %.0fx%.0f)", $0.title, $0.desc, $0.x, $0.y, $0.width, $0.height)
        }
        lines.append("[tree]")
        lines += ax.describeMCTree(group, maxDepth: 7)
        lines.append("[on-screen windows]")
        for w in WindowList.query([.optionOnScreenOnly]) {
            let b = w.bounds ?? .zero
            lines.append("  \(w.owner) pid=\(w.pid) layer=\(w.layer) alpha=\(w.alpha) name='\(w.name)' (\(b.origin.x),\(b.origin.y) \(b.width)x\(b.height))")
        }
        let text = lines.joined(separator: "\n") + "\n"
        if diagCount == 1 {
            try? text.write(to: Log.diagFile, atomically: true, encoding: .utf8)
        } else {
            Log.append(text, to: Log.diagFile)
        }
    }
}
