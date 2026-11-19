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
