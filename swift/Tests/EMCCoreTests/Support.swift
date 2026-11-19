import ApplicationServices
@testable import EMCCore
import Foundation

/// Stands in for the live system: Mission Control's AX tree and the window
/// lookups are fed from properties, so the controller's logic can run
/// headless (no Mission Control, no screen, no mouse).
final class StubAX: AXBridge {
    var snapshot: (thumbs: [Tile], spaces: [Tile], windowCount: Int) = ([], [], 1)
    var group: AXUIElement?
    var titlePid: pid_t?
    var actionCaps: WindowCaps?
    var windows: [WindowInfo] = []
    var listCalls: [Bool] = []
    var closeResults: [Bool] = []
    var closeCalls: [(String, [pid_t])] = []
    var activated: [pid_t] = []
    var windowNumberValue: CGWindowID?
    var button: AXUIElement?
    var foundWindow: AXUIElement?
    var findCalls: [(String, [pid_t])] = []
    var events: [String] = []
    var pressResult = true
    var attributeResult: (AXError, AnyObject?) = (.success, nil)

    init() { super.init(backdrop: BackdropRule(major: 26, defaults: UserDefaults(suiteName: "EMCTests")!)) }

    override func missionControlSnapshot(_ group: AXUIElement?) -> (thumbs: [Tile], spaces: [Tile], windowCount: Int) {
        snapshot
    }
    override func missionControlGroup() -> AXUIElement? { group }
    override func missionControlProbablyActive() -> Bool { true }
    override func dockAppItems() -> [Tile] { [] }
    override func pidForTitle(_ title: String) -> pid_t? { titlePid }
    override func actionCapsFor(_ pid: pid_t?, _ title: String) -> WindowCaps {
        actionCaps ?? super.actionCapsFor(pid, title)
    }
    override func listWindows(excludePid: pid_t? = nil, allSpaces: Bool = false) -> [WindowInfo] {
        listCalls.append(allSpaces)
        return windows
    }
    override func closeWindowByTitle(_ title: String, _ pids: [pid_t], foreground: Bool = false) -> Bool {
        if closeResults.isEmpty { return super.closeWindowByTitle(title, pids, foreground: foreground) }
        closeCalls.append((title, pids))
        return closeResults.removeFirst()
    }
    override func activatePid(_ pid: pid_t) -> Bool {
        activated.append(pid)
        return true
    }
    override func windowNumber(_ window: AXUIElement?) -> CGWindowID? { windowNumberValue }
    override func buttonOf(_ window: AXUIElement?, _ attr: String) -> AXUIElement? { button }
    override func foregroundWindow(_ window: AXUIElement) -> pid_t? {
        events.append("raise")
        return 1
    }
    override func pressElement(_ element: AXUIElement?) -> Bool {
        events.append("press")
        return pressResult
    }
    override func findWindowByTitle(_ title: String, _ pids: [pid_t]) -> AXUIElement? {
        findCalls.append((title, pids))
        return foundWindow
    }
    override func copyAttribute(_ element: AXUIElement, _ name: String) -> (AXError, AnyObject?) {
        attributeResult
    }
}

/// A controller whose delayed self-messages are recorded, not run.
class TestController: MissionControlController {
    var scheduled: [Selector] = []
    override func schedule(_ selector: Selector, after delay: Double) { scheduled.append(selector) }
}

/// Distinct fake AX elements (application elements for unused pids).
func fakeElement(_ n: Int32) -> AXUIElement { AXUIElementCreateApplication(90_000 + n) }

let screenW = 1470.0, screenH = 956.0

func makeController(_ ax: StubAX = StubAX()) -> TestController {
    let c = TestController(ax: ax)
    c.screenWidth = screenW
    c.screenHeight = screenH
    return c
}

func thumb(x: Double = 100, title: String = "Songs") -> Tile {
    Tile(title: title, x: x, y: 300, width: 400, height: 260)
}

/// A Spaces Bar tile: full-size at y=46 when settled (the commit state), or
/// the scaled-down off-screen strip seen throughout a drag gesture.
func barTile(settled: Bool = true) -> Tile {
    settled ? Tile(title: "Desktop", x: 411, y: 46, width: 138, height: 90)
        : Tile(title: "Desktop", x: 582, y: -32, width: 65, height: 24)
}

func sig(_ n: Int) -> [WindowSig] { [WindowSig(number: n, x: n, y: 0, w: 10, h: 10)] }

/// One collectTargets pass against a synthetic layout (window controls only:
/// Show Desktop is independent).
@discardableResult
func frame(_ c: MissionControlController, _ thumbs: [Tile], _ spaces: [Tile]? = nil,
           cg: [WindowSig]? = nil) -> (targets: [Target], marks: [Mark]) {
    if let cg = cg { c.cgSig = cg }
    (c.ax as! StubAX).snapshot = (thumbs, spaces ?? [barTile()], 1)
    let (targets, marks) = c.collectTargets(nil)
    return (targets.filter { $0.kind != .desktop }, marks)
}

/// Backdate the settle candidate past settleConfirm.
func confirmSettle(_ c: MissionControlController, _ extra: Double = Tuning.settleConfirm) {
    c.candidateSince -= extra + 0.05
}

/// Fingers touching the trackpad (touch frames streaming).
func fingersDown(_ c: MissionControlController) { c.lastTouch = Clock.now() }
