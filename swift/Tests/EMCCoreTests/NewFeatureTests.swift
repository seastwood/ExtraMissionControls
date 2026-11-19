@testable import EMCCore
import XCTest

final class SettingsTests: XCTestCase {
    private let keys = ["EMCShow.minimize", "EMCShow.makefullscreen", "EMCShortcuts", "EMCShortcut.close",
                        "EMCShortcut.minimize", "EMCButtonSize"]

    override func tearDown() {
        keys.forEach { UserDefaults.standard.removeObject(forKey: $0) }
    }

    func testHiddenButtonsLeaveNoGap() {
        Settings.setShows(.minimize, false)
        Settings.setShows(.makeFullscreen, false)
        let c = makeController()
        frame(c, [thumb()])
        let row = frame(c, [thumb()]).targets
        XCTAssertEqual(row.map { $0.kind }, [.window, .arrange, .quit])
        XCTAssertEqual(row.map { $0.tile.slot }, [0, 1, 2])
    }

    func testShortcutKeysFollowSettings() {
        XCTAssertEqual(Settings.shortcutAction(forKeycode: 13), .close, "W closes by default")
        XCTAssertEqual(Settings.shortcutAction(forKeycode: 46), .minimize)
        XCTAssertEqual(Settings.shortcutAction(forKeycode: 12), .quit)
        Settings.setShortcutLetter(.close, "x")
        XCTAssertEqual(Settings.shortcutAction(forKeycode: 7), .close, "rebinding to X")
        XCTAssertNil(Settings.shortcutAction(forKeycode: 13), "W is free again")
        Settings.shortcutsEnabled = false
        XCTAssertNil(Settings.shortcutAction(forKeycode: 46))
    }

    func testButtonSizeIsClamped() {
        XCTAssertEqual(Settings.buttonSize, Settings.defaultButtonSize)
        Settings.buttonSize = 100
        XCTAssertEqual(Settings.buttonSize, Settings.buttonSizeRange.upperBound)
    }
}

final class DetectionTrustTests: XCTestCase {
    func testObserverEarnsAndLosesTrust() {
        let c = makeController()
        XCTAssertEqual(c.idleDetectInterval, Tuning.detectInterval)
        for _ in 0..<Tuning.observerHitsToTrust {
            c.lastObserverEvent = Clock.now()
            c.noteDetection()
        }
        XCTAssertTrue(c.observerTrusted)
        XCTAssertEqual(c.idleDetectInterval, Tuning.detectIntervalAssisted, "trusted notifications slow the poll")
        c.lastObserverEvent = Clock.now() - 5
        c.noteDetection()
        XCTAssertFalse(c.observerTrusted, "one opening found without a notification restores the fast poll")
        XCTAssertEqual(c.idleDetectInterval, Tuning.detectInterval)
    }
}

final class TypedActionTests: XCTestCase {
    func testActionsKeepPythonNamesAndGlyphs() {
        XCTAssertEqual(WindowAction.forceQuit.rawValue, "forcequit")
        XCTAssertEqual(WindowAction.snapMax.region, .max)
        XCTAssertEqual(WindowAction.minimize.markGlyph, "−")
        XCTAssertTrue(WindowAction.close.isWindowButton)
        XCTAssertFalse(WindowAction.quit.isWindowButton)
        XCTAssertEqual(ButtonKind.makeFullscreen.rawValue, "makefullscreen")
        XCTAssertTrue(ButtonKind.arrange.hasMenu)
        XCTAssertEqual(DockActionKind.closeAll.glyph, "✕")
    }

    func testMenuPickRoutesWindowActions() {
        let c = makeController()
        c.menuOpen = true
        c.menuKind = .quit
        c.menuTitle = "Doc"
        c.menuActions = Tuning.quitMenuActions
        c.menuPick(1)
        XCTAssertEqual(c.deferredActions, [DeferredAction(action: .forceQuit, title: "Doc")])
        XCTAssertEqual(c.marked["Doc"], "⏼")
    }
}

/// macOS 27: an app dragged down out of the Spaces Bar keeps the id of its
/// destroyed full-screen space on its thumbnail until Mission Control reopens.
final class SpaceFilterTests: XCTestCase {
    func t(_ title: String, _ space: Int?) -> Tile {
        Tile(title: title, x: 100, y: 300, width: 400, height: 260, space: space)
    }

    func testDroppedOutOfFullScreenKeepsItsThumbnail() {
        // Desktop is space 1; space 7 (the app's full-screen space) is gone.
        let thumbs = [t("Notes", 1), t("Safari", 7)]
        let r = AXBridge.filterToShownSpace(thumbs, [], active: [1], spaceTypes: [1: 0, 3: 4],
                                            verified: true)
        XCTAssertEqual(r.thumbs.map { $0.title }, ["Notes", "Safari"])
    }

    func testOnlyWindowOnDesktopAfterDropIsKept() {
        let r = AXBridge.filterToShownSpace([t("Safari", 7)], [], active: [1], spaceTypes: [1: 0],
                                            verified: true)
        XCTAssertEqual(r.thumbs.map { $0.title }, ["Safari"], "not mistaken for an empty desktop's ghost")
    }

    func testOtherLiveFullScreenSpacesStillFiltered() {
        // Space 3 is a live full-screen space: its windows are the "ghosts".
        let r = AXBridge.filterToShownSpace([t("Notes", 1), t("Ghost", 3)], [], active: [1],
                                            spaceTypes: [1: 0, 3: 4], verified: false)
        XCTAssertEqual(r.thumbs.map { $0.title }, ["Notes"])
        XCTAssertTrue(r.verified)
        let empty = AXBridge.filterToShownSpace([t("Ghost", 3)], [], active: [1], spaceTypes: [1: 0, 3: 4],
                                                verified: true)
        XCTAssertTrue(empty.thumbs.isEmpty, "an empty desktop still hides other spaces' windows")
    }
}
