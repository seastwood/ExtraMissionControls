import ApplicationServices
@testable import EMCCore
import XCTest

/// Ports of scripts/test_action_identity.py and test_deferred_spaces.py, plus
/// coverage for the pure helpers.
final class ActionIdentityTests: XCTestCase {
    func testCaptureAndAmbiguousTitles() {
        let ax = StubAX()
        let first = fakeElement(1), second = fakeElement(2)
        ax.titlePid = 12
        ax.windowRefs = [12: [(title: "Doc", window: first, number: 1), (title: "Doc", window: second, number: 2)]]
        XCTAssertNil(ax.captureWindow("Doc"))
        let key = "Doc" + AXBridge.keySep + "1"
        XCTAssertNil(ax.captureWindow(key), "an unresolved twin never reduces to a plain title")
        ax.pinned[key] = (12, 2)
        let captured = ax.captureWindow(key)
        XCTAssertEqual(captured?.pid, 12)
        XCTAssertEqual(captured?.number, 2)
        XCTAssertTrue(captured.map { CFEqual($0.window, second) } ?? false)
    }

    func testRetainedIdentitySurvivesRetitle() {
        let ax = StubAX()
        ax.windowNumberValue = 7
        ax.button = fakeElement(3)
        let target = CapturedWindow(pid: 12, window: fakeElement(4), number: 7)
        XCTAssertTrue(ax.performCapturedAction(target, "close"))
        XCTAssertEqual(ax.events, ["raise", "press"])
        XCTAssertTrue(ax.findCalls.isEmpty, "must not search again")
    }

    func testReplacedIdentityIsNotPressed() {
        let ax = StubAX()
        ax.windowNumberValue = 8
        ax.button = fakeElement(3)
        XCTAssertFalse(ax.performCapturedAction(CapturedWindow(pid: 12, window: fakeElement(4), number: 7), "close"))
        XCTAssertFalse(ax.events.contains("press"))
    }

    func testCompletionRequiresObservation() {
        let ax = StubAX()
        let target = CapturedWindow(pid: 12, window: fakeElement(4), number: 7)
        ax.attributeResult = (.success, "AXWindow" as CFString)
        XCTAssertEqual(ax.capturedActionState(target, "close"), false)
        ax.attributeResult = (.invalidUIElement, nil)
        XCTAssertEqual(ax.capturedActionState(target, "close"), true)
        XCTAssertNil(ax.capturedActionState(target, "minimize"))
        ax.attributeResult = (.success, kCFBooleanTrue)
        XCTAssertEqual(ax.capturedActionState(target, "minimize"), true)
    }

    func testCancelDropsRetainedTarget() {
        let c = makeController()
        c.deferredActions = [DeferredAction(action: "close", title: "Doc")]
        c.actionTargets["Doc"] = CapturedWindow(pid: 12, window: fakeElement(4), number: 7)
        c.actionStarted["Doc"] = 1
        c.deferredRetries["Doc"] = 1
        c.cancelDeferredAction("Doc")
        XCTAssertTrue(c.actionTargets.isEmpty && c.actionStarted.isEmpty && c.deferredRetries.isEmpty
                      && c.deferredActions.isEmpty)
    }

    func testCapabilityFilterReflowsButtons() {
        let ax = StubAX()
        ax.actionCaps = WindowCaps(first: false, second: false)
        let c = makeController(ax)
        frame(c, [thumb()])
        let kinds = frame(c, [thumb()]).targets.map { $0.kind }
        XCTAssertFalse(kinds.contains("window"))
        XCTAssertFalse(kinds.contains("minimize"))
        XCTAssertTrue(kinds.contains("quit"))
        XCTAssertEqual(frame(c, [thumb()]).targets.first?.tile.slot, 0, "remaining buttons slide left")
    }
}

final class DeferredSpacesTests: XCTestCase {
    func testDeferredActionSurvivesSpaceReturn() {
        let ax = StubAX()
        ax.windows = [WindowInfo(windowID: 1, pid: 42, owner: "Editor", title: "Desktop document",
                                 x: 0, y: 0, width: 800, height: 600)]
        ax.closeResults = [false, true]
        ax.titlePid = 42
        let c = makeController(ax)
        c.deferredActions = [DeferredAction(action: "close", title: "Desktop document")]
        c.applyDeferredActions()
        XCTAssertEqual(c.deferredActions, [DeferredAction(action: "close", title: "Desktop document")])
        XCTAssertEqual(ax.activated, [42])
        XCTAssertTrue(c.tlQueue.isEmpty)
        c.applyDeferredActions()
        XCTAssertTrue(c.deferredActions.isEmpty && c.deferredRetries.isEmpty)
        XCTAssertTrue(ax.listCalls.allSatisfy { $0 }, "every lookup spans all spaces")
        XCTAssertEqual(ax.closeCalls.last?.0, "Desktop document")
        XCTAssertEqual(ax.closeCalls.last?.1, [42])
    }

    func testForegroundPathRaisesThenPressesOneWindow() {
        let ax = StubAX()
        ax.foundWindow = fakeElement(5)
        ax.button = fakeElement(6)
        XCTAssertTrue(ax.closeWindowByTitle("Document", [42], foreground: true))
        XCTAssertEqual(ax.events, ["raise", "press"])
        XCTAssertEqual(ax.findCalls.count, 1)
        XCTAssertEqual(ax.findCalls.first?.0, "Document")
        ax.events = []
        XCTAssertTrue(ax.minimizeWindowByTitle("Document", [42], foreground: true))
        XCTAssertEqual(ax.events, ["raise", "press"])
    }
}

final class HelperTests: XCTestCase {
    func testThumbIDSplitting() {
        let parts = AXBridge.splitThumbID("com.apple.Safari.space.3")
        XCTAssertEqual(parts.bundle, "com.apple.Safari")
        XCTAssertEqual(parts.space, 3)
        XCTAssertNil(AXBridge.splitThumbID("mc.display").bundle)
        XCTAssertEqual(AXBridge.baseTitle("Doc" + AXBridge.keySep + "2"), "Doc")
        XCTAssertTrue(AXBridge.titlesRelated("Songs", "Songs – 1 note"))
    }

    func testMatchLayoutKeepsRelativePositions() {
        let thumbs = [Rect(500, 100, 200, 150), Rect(100, 100, 200, 150), Rect(300, 400, 150, 300)]
        let windows = [Rect(0, 0, 800, 600), Rect(900, 0, 800, 600), Rect(400, 700, 600, 1200)]
        XCTAssertEqual(AXBridge.matchLayout(thumbs, windows), [1, 0, 2])
    }

    func testUpdateVersionParsing() {
        XCTAssertEqual(Updates.parse("EMC_0.9.2"), [0, 9, 2])
        XCTAssertEqual(Updates.parse("v0.10.3"), [0, 10, 3])
        XCTAssertNil(Updates.parse("beta"))
        XCTAssertTrue(Updates.isNewer([0, 11, 0], than: [0, 10, 3]))
        XCTAssertFalse(Updates.isNewer([0, 10, 3], than: [0, 10, 3]))
        XCTAssertTrue(Updates.isNewer([0, 10, 3, 1], than: [0, 10, 3]))
    }

    func testSlotAssignmentIsStable() {
        let previous = ["a": 0, "b": 1, "c": 2]
        // "b" went away; "a" and "c" keep their panels, the newcomer takes one
        // that was not on screen last frame.
        XCTAssertEqual(assignSlots(["a", "c", "d"], previous: previous) { _, _ in false }, [0, 2, 3])
    }

    func testLegacyBackdropRule() {
        XCTAssertFalse(legacyBackdropRule([OwnerLayer(owner: "Dock", layer: 20): 1]), "resident fullscreen window")
        XCTAssertTrue(legacyBackdropRule([OwnerLayer(owner: "Dock", layer: 18): 1]))
        XCTAssertTrue(legacyBackdropRule([OwnerLayer(owner: "Dock", layer: 20): 2]))
    }

    func testLearnedBackdropRule() {
        let defaults = UserDefaults(suiteName: "EMCBackdropTest")!
        defaults.removePersistentDomain(forName: "EMCBackdropTest")
        let rule = BackdropRule(major: 27, defaults: defaults)
        XCTAssertEqual(rule.mode, .unknown)
        let idle = [OwnerLayer(owner: "WindowManager", layer: 20): 1]
        XCTAssertTrue(rule.idleShouldCheck(idle, now: 0))
        rule.idleCheckFoundNothing()
        let up = [OwnerLayer(owner: "WindowManager", layer: 20): 2]
        rule.confirmed(up)
        XCTAssertEqual(rule.mode, .learned)
        XCTAssertTrue(rule.idleShouldCheck(up, now: 1), "the learned window count fires")
        XCTAssertFalse(rule.idleShouldCheck(idle, now: 1), "idle counts don't (before the AX fallback)")
    }
}
