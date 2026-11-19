import ApplicationServices
@testable import EMCCore
import XCTest

/// Ports of scripts/test_exit_latch.py, test_zoom_suppression.py,
/// test_untitled_filter.py and test_input_dispatch.py.
final class ExitLatchTests: XCTestCase {
    func testLatchMovementExpiryAndGate() {
        let c = makeController()
        XCTAssertTrue(frame(c, [thumb()]).targets.isEmpty, "first frame is the baseline")
        XCTAssertEqual(frame(c, [thumb()]).targets.count, 5, "settled layout emits window buttons")

        // A latch while the layout stays frozen: hidden.
        c.latchExit()
        var r = frame(c, [thumb()])
        XCTAssertTrue(r.targets.isEmpty && r.marks.isEmpty, "latched + frozen layout -> nothing")
        r = frame(c, [thumb()])
        XCTAssertTrue(r.targets.isEmpty && r.marks.isEmpty, "still latched next frame")

        // Movement releases it; the moved layout must first earn trust.
        frame(c, [thumb(x: 140)])
        XCTAssertFalse(c.exitLatched, "movement releases the latch")
        fingersDown(c)
        XCTAssertTrue(frame(c, [thumb(x: 140)]).targets.isEmpty, "new layout needs settle confirmation first")
        confirmSettle(c)
        fingersDown(c)
        XCTAssertEqual(frame(c, [thumb(x: 140)]).targets.count, 5, "buttons return once confirmed")

        // ...and so does expiry, even with the layout still frozen.
        c.latchExit()
        XCTAssertTrue(frame(c, [thumb(x: 140)]).targets.isEmpty, "latched again (frozen)")
        c.exitLatchExpires = Clock.now() - 0.01
        XCTAssertEqual(frame(c, [thumb(x: 140)]).targets.count, 5, "expiry releases a frozen latch")
        XCTAssertFalse(c.exitLatched)

        // Scene gate.
        XCTAssertGreaterThanOrEqual(Tuning.sceneStreakMin, 1)
        c.sceneStreak = 0
        r = frame(c, [thumb(x: 140)])
        XCTAssertTrue(r.targets.isEmpty && r.marks.isEmpty, "closed scene gate emits nothing")
        c.sceneStreak = Tuning.sceneStreakMin
        XCTAssertEqual(frame(c, [thumb(x: 140)]).targets.count, 5, "reopened scene gate emits again")
    }

    func testPreBaselineLatchAdoptsFirstLayout() {
        let c = makeController()
        c.latchExit()
        XCTAssertTrue(frame(c, [thumb()]).targets.isEmpty, "pre-baseline latch frame stays hidden")
        XCTAssertTrue(frame(c, [thumb()]).targets.isEmpty)
        XCTAssertNotNil(c.exitLatchSig, "adopted signature keeps it latched while frozen")
        frame(c, [thumb(x: 200)])
        XCTAssertFalse(c.exitLatched, "adopted signature still releases on movement")
    }

    func testPausedGestureNeverRendersUntilBarCommits() {
        let c = makeController()
        frame(c, [thumb()], [barTile(settled: false)])
        var targets: [Target] = []
        for _ in 0..<6 {
            fingersDown(c)
            targets = frame(c, [thumb()], [barTile(settled: false)]).targets
        }
        XCTAssertTrue(targets.isEmpty, "paused mid-gesture (bar unsettled) never renders")
        XCTAssertEqual(frame(c, [thumb()], [barTile(settled: true)]).targets.count, 5,
                       "bar snapping settled = the commit -> buttons render")
    }

    func testJumpCommitRendersNextStillFrame() {
        let c = makeController()
        frame(c, [thumb(x: 100)], [barTile(settled: false)])
        frame(c, [thumb(x: 140)], [barTile(settled: false)])
        XCTAssertTrue(frame(c, [thumb(x: 300)], [barTile(settled: true)]).targets.isEmpty)
        XCTAssertEqual(frame(c, [thumb(x: 300)], [barTile(settled: true)]).targets.count, 5,
                       "first still frame after a jump-commit renders (no 0.4s wait)")
    }

    func testCommitEaseWithCollapsedBar() {
        let c = makeController()
        fingersDown(c); frame(c, [thumb(x: 100)], [barTile(settled: false)], cg: sig(1))
        fingersDown(c); frame(c, [thumb(x: 140)], [barTile(settled: false)], cg: sig(2))
        fingersDown(c)
        XCTAssertTrue(frame(c, [thumb(x: 300)], [barTile(settled: false)], cg: sig(3)).targets.isEmpty)
        fingersDown(c)
        XCTAssertEqual(frame(c, [thumb(x: 300)], [barTile(settled: false)], cg: sig(4)).targets.count, 5,
                       "AX-still + windows-easing = commit")
    }

    func testFrozenHoldStaysHidden() {
        let c = makeController()
        for _ in 0..<2 {
            fingersDown(c)
            frame(c, [thumb(x: 100)], [barTile(settled: false)], cg: sig(1))
        }
        fingersDown(c)
        XCTAssertTrue(frame(c, [thumb(x: 100)], [barTile(settled: false)], cg: sig(1)).targets.isEmpty)
    }

    func testAcceptanceDropsSyncCadence() {
        let c = makeController()
        c.syncInterval = Tuning.syncIntervalFast
        fingersDown(c); frame(c, [thumb()], [barTile(settled: false)])
        fingersDown(c); frame(c, [thumb()], [barTile(settled: false)])
        XCTAssertEqual(c.syncInterval, Tuning.syncIntervalFast, "still fast while unsettled")
        frame(c, [thumb()], [barTile(settled: true)])
        XCTAssertEqual(c.syncInterval, Tuning.syncInterval, "acceptance drops the cadence to normal")
    }

    func testFlickRendersImmediatelyWhenFingersUp() {
        let c = makeController()
        fingersDown(c)
        frame(c, [thumb(x: 100)], [barTile(settled: false)])
        c.lastTouch = Clock.now() - 1.0
        frame(c, [thumb(x: 200)], [barTile(settled: false)])
        frame(c, [thumb(x: 290)], [barTile(settled: false)])
        XCTAssertTrue(frame(c, [thumb(x: 300)], [barTile(settled: false)]).targets.isEmpty)
        XCTAssertEqual(frame(c, [thumb(x: 300)], [barTile(settled: false)]).targets.count, 5)
    }

    func testReflowGlide() {
        let c = makeController()
        frame(c, [thumb(x: 100)])
        XCTAssertEqual(frame(c, [thumb(x: 100)]).targets.count, 5)
        c.recentlyClosed = ["acted|Tile": Clock.now() + 2]
        XCTAssertEqual(frame(c, [thumb(x: 160)]).targets.count, 5, "re-flow glide keeps buttons while moving")
        XCTAssertEqual(frame(c, [thumb(x: 220)]).targets.count, 5, "glide continues")
        XCTAssertEqual(frame(c, [thumb(x: 220)]).targets.count, 5, "glide hands off at rest")

        let g = makeController()
        frame(g, [thumb(x: 100)]); frame(g, [thumb(x: 100)])
        g.recentlyClosed = ["acted|Tile": Clock.now() + 2]
        fingersDown(g)
        XCTAssertTrue(frame(g, [thumb(x: 160)]).targets.isEmpty, "fingers down = gesture: hidden")

        let n = makeController()
        frame(n, [thumb(x: 100)]); frame(n, [thumb(x: 100)])
        XCTAssertTrue(frame(n, [thumb(x: 160)]).targets.isEmpty, "motion with no recent action hides")
    }

    func testMotionTolerance() {
        let a = [WindowSig(number: 1, x: 100, y: 100, w: 400, h: 300), WindowSig(number: 2, x: 600, y: 100, w: 400, h: 300)]
        let lift = [WindowSig(number: 1, x: 92, y: 91, w: 416, h: 318), WindowSig(number: 2, x: 600, y: 100, w: 400, h: 300)]
        let big = [WindowSig(number: 1, x: 100, y: 250, w: 400, h: 300), WindowSig(number: 2, x: 600, y: 100, w: 400, h: 300)]
        let gone = [WindowSig(number: 1, x: 100, y: 100, w: 400, h: 300)]
        XCTAssertFalse(Motion.moved(a, lift), "hover-lift drift is not motion")
        XCTAssertTrue(Motion.moved(a, big), "large delta is motion")
        XCTAssertTrue(Motion.moved(a, gone), "vanished window is motion")
        XCTAssertTrue(Motion.moved(gone, a), "appeared window is motion")
        XCTAssertFalse(Motion.moved(nil, a), "nil baselines are not motion")
    }

    func testLongFrozenHoldFallback() {
        let c = makeController()
        fingersDown(c); frame(c, [thumb()], [barTile(settled: false)])
        fingersDown(c); frame(c, [thumb()], [barTile(settled: false)])
        confirmSettle(c, Tuning.settleConfirmNoBar)
        fingersDown(c)
        XCTAssertEqual(frame(c, [thumb()], [barTile(settled: false)]).targets.count, 5)
    }
}

final class ZoomSuppressionTests: XCTestCase {
    func tile(_ x: Double, _ y: Double, _ w: Double, _ h: Double) -> Tile {
        Tile(title: "Win", x: x, y: y, width: w, height: h)
    }

    func testZoomTransients() {
        let c = makeController()
        let fullscreen = tile(0, 0, screenW, screenH)
        XCTAssertTrue(c.zooming(fullscreen))
        XCTAssertTrue(c.zooming(tile(10, 10, screenW * 0.94, screenH * 0.93)))
        XCTAssertTrue(c.zooming(tile(20, 20, screenW * 0.88, screenH * 0.88)))
        let single = tile(150, 200, screenW * 0.72, screenH * 0.70)
        XCTAssertFalse(c.zooming(single))
        let half = tile(120, 210, screenW * 0.36, screenH * 0.70)
        XCTAssertFalse(c.zooming(half))
        XCTAssertFalse(c.zooming(tile(0, 400, screenW, screenH * 0.5)), "needs BOTH dimensions")
        XCTAssertFalse(MissionControlController(ax: StubAX()).zooming(fullscreen), "no screen size => never")
        XCTAssertTrue([fullscreen].contains(where: c.zooming))
        XCTAssertTrue([single, fullscreen].contains(where: c.zooming))
        XCTAssertFalse([single, half].contains(where: c.zooming))
        let panned = tile(-screenW, 200, screenW * 0.72, screenH * 0.70)
        XCTAssertEqual([single, panned].filter(c.onScreenTile).map { $0.x }, [single.x])
    }

    func testZoomGateSuppressesEverything() {
        let c = makeController()
        let full = Tile(title: "Big", x: 0, y: 0, width: screenW, height: screenH)
        frame(c, [thumb(), full])
        XCTAssertTrue(frame(c, [thumb(), full]).targets.isEmpty)
    }
}

final class UntitledFilterTests: XCTestCase {
    func testUntitledThumbnailsGetNothing() {
        let c = makeController()
        XCTAssertTrue(c.untitled(Tile(title: "")))
        XCTAssertTrue(c.untitled(Tile(title: "  ")))
        XCTAssertFalse(c.untitled(Tile(title: "Songs")))
        c.deferWindowAction("close", "")
        c.deferWindowAction("quit", "   ")
        XCTAssertTrue(c.deferredActions.isEmpty && c.marked.isEmpty, "empty-title actions are refused")
        c.deferWindowAction("close", "Songs")
        XCTAssertEqual(c.deferredActions, [DeferredAction(action: "close", title: "Songs")])
        XCTAssertEqual(c.marked["Songs"], "✕")
        XCTAssertFalse(AXBridge.titlesRelated("", "Songs"))
        XCTAssertFalse(AXBridge.titlesRelated("Songs", ""))
        XCTAssertFalse(AXBridge().closeWindowByTitle("", []))
    }

    func testUntitledThumbGetsNoButtons() {
        let c = makeController()
        frame(c, [thumb(title: "")])
        XCTAssertTrue(frame(c, [thumb(title: "")]).targets.isEmpty)
    }
}

final class InputDispatchTests: XCTestCase {
    final class RecordingController: TestController {
        var calls: [String] = []
        override func closeIndex(_ index: Int) { calls.append("close \(index)") }
        override func openMenuFor(_ index: Int, viaHover: Bool = false) { calls.append("menu \(index)") }
    }

    func testQueuedClicksNeverActOnReplacements() {
        let c = RecordingController(ax: StubAX())
        c.active = true
        let target = ButtonTarget(kind: "window", title: "Original", element: nil)
        c.targets = [0: target]
        c.rects = [0: Rect(100, 100, 24, 24)]
        c.dispatchButton(0, target, menu: false)
        c.dispatchButton(0, target, menu: true)
        XCTAssertEqual(c.calls, ["close 0", "menu 0"])
        c.calls = []

        c.targets[0] = ButtonTarget(kind: "window", title: "Replacement", element: nil)
        c.dispatchButton(0, target, menu: false)
        c.dispatchButton(0, target, menu: true)
        XCTAssertTrue(c.calls.isEmpty, "a queued click must never act on a replacement window")

        c.targets[0] = target
        c.rects = [:]
        c.dispatchButton(0, target, menu: false)
        XCTAssertTrue(c.calls.isEmpty, "hidden controls must reject queued clicks")

        c.rects[0] = Rect(100, 100, 24, 24)
        c.active = false
        c.dispatchButton(0, target, menu: true)
        XCTAssertTrue(c.calls.isEmpty, "exited Mission Control must reject queued menus")
    }

    func testUndoBypassesQuietHeartbeat() {
        let c = makeController()
        c.deferredActions = [DeferredAction(action: "close", title: "Original")]
        c.marked = ["Original": "✕"]
        c.watchSig = [WindowSig(number: 1, x: 100, y: 100, w: 400, h: 300)]
        c.cancelDeferredAction("Original")
        XCTAssertNil(c.watchSig, "undo must bypass the quiet heartbeat")
        XCTAssertTrue(c.deferredActions.isEmpty && c.marked.isEmpty)
    }

    func testShortcutTogglesDeferredAction() {
        let c = makeController()
        c.shortcutAction("close", "Doc")
        XCTAssertEqual(c.deferredActions.map { $0.action }, ["close"])
        c.shortcutAction("close", "Doc")
        XCTAssertTrue(c.deferredActions.isEmpty, "pressing the same key again takes it back")
    }
}
