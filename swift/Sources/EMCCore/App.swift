import AppKit
import Foundation
import Sparkle

/// Menu-bar app shell: status item, permission prompt, and the run loop.
public enum EMCApp {
    private static var delegate: AppDelegate?  // NSApplication.delegate is weak

    public static func run() {
        replaceRunningInstance()
        CrashGuard.install()
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)  // menu-bar app: no Dock icon
        let delegate = AppDelegate()
        self.delegate = delegate
        app.delegate = delegate
        // Ctrl+C / kill quit immediately.
        for sig in [SIGINT, SIGTERM, SIGHUP] { signal(sig) { _ in exit(0) } }
        app.run()
    }

    static let pidFile = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("extramissioncontrols.pid")

    /// Only one EMC may run: two draw duplicate buttons and tray icons. Stop
    /// any other copy — this build, or the Python version (same bundle id when
    /// built, `main.py` when run from source).
    static func replaceRunningInstance() {
        let me = ProcessInfo.processInfo.processIdentifier
        var others: [pid_t] = []
        if let bundle = Bundle.main.bundleIdentifier {
            others += NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
                .map { $0.processIdentifier }.filter { $0 != me }
        }
        if let text = try? String(contentsOf: pidFile, encoding: .utf8),
           let old = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)), old != me, old > 0,
           isEMCProcess(old) {
            others.append(old)
        }
        for pid in Set(others) {
            kill(pid, SIGCONT)  // a suspended (Ctrl+Z) copy can't exit otherwise
            kill(pid, SIGTERM)
            var gone = false
            for _ in 0..<20 {  // up to 1s for a clean exit, then force it
                if kill(pid, 0) != 0 { gone = true; break }
                usleep(50_000)
            }
            if !gone { kill(pid, SIGKILL) }
        }
        try? String(me).write(to: pidFile, atomically: true, encoding: .utf8)
    }

    /// Only stop a pid that really is EMC (pids get reused).
    private static func isEMCProcess(_ pid: pid_t) -> Bool {
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/bin/ps")
        task.arguments = ["-p", String(pid), "-o", "command="]
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        guard (try? task.run()) != nil else { return false }
        task.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return out.contains("main.py") || out.contains("ExtraMissionControls") || out.contains("Extra Mission Controls")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    static let updateCheckInterval = 6 * 3600.0

    var missionControl: MissionControlController!
    var statusItem: NSStatusItem!
    var glassItem: NSMenuItem!
    var colorsItem: NSMenuItem!
    var loginItem: NSMenuItem!
    var updateItem: NSMenuItem?
    var updateSeparator: NSMenuItem?
    var updateURL: String?
    var updateVersion: String?
    var updateTimer: Timer?
    var updater: SPUStandardUpdaterController?
    var crashItem: NSMenuItem?
    let settingsWindow = SettingsWindowController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        LoginItem.migrateLegacyAgent()
        missionControl = MissionControlController()
        missionControl.start()
        settingsWindow.model.onStyleChange = { [weak self] in self?.missionControl.rebuildStyle() }
        if Updates.sparkleConfigured {
            // Sparkle schedules its own checks and installs updates itself.
            updater = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil,
                                                   userDriverDelegate: nil)
        }
        setupStatusItem()
        if updater == nil {
            // Release notifier: first check shortly after launch, then every 6h.
            perform(#selector(checkForUpdates(_:)), with: nil, afterDelay: 10)
            updateTimer = Timer.scheduledTimer(timeInterval: Self.updateCheckInterval, target: self,
                                               selector: #selector(checkForUpdates(_:)), userInfo: nil,
                                               repeats: true)
        }
        if CrashGuard.relaunchedAfterCrash() {
            Log.error("relaunch", "restarted after a crash; see Show Crash Reports in the menu")
        }
        if !missionControl.ax.ensurePermissions() {
            Log.note("Accessibility permission missing — nothing will work until it is granted "
                     + "(System Settings → Privacy & Security → Accessibility).")
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        missionControl?.stop()
    }

    private func trayImage() -> NSImage? {
        let url = Bundle.main.url(forResource: "tray_icon", withExtension: "png")
            ?? URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
                .appendingPathComponent("tray_icon.png")
        guard let image = NSImage(contentsOf: url) else { return nil }
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = true  // tinted for light and dark menu bars
        image.accessibilityDescription = "Extra Mission Controls"
        return image
    }

    /// A menu item with an SF Symbol in the ✓/state slot: on macOS 26 a regular
    /// item image gets its own indented column and pushes the title right.
    private func symbolItem(_ title: String, _ action: Selector, _ symbol: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        if let image = NSImage(systemSymbolName: symbol, accessibilityDescription: title) {
            let sized = image.withSymbolConfiguration(.init(pointSize: 12, weight: .regular)) ?? image
            item.state = .on
            item.onStateImage = sized
        }
        return item
    }

    private func toggleItem(_ title: String, _ action: Selector, _ on: Bool) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.state = on ? .on : .off
        return item
    }

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = trayImage() {
            statusItem.button?.image = image
        } else {
            statusItem.button?.title = "EMC"
        }
        let menu = NSMenu()
        let info = NSMenuItem(title: "Extra Mission Controls \(EMCVersion.current)", action: nil, keyEquivalent: "")
        info.isEnabled = false
        menu.addItem(info)
        menu.addItem(.separator())
        glassItem = toggleItem("Liquid Glass", #selector(toggleLiquidGlass(_:)), UI.liquidGlassEnabled)
        menu.addItem(glassItem)
        colorsItem = toggleItem("Colors", #selector(toggleColors(_:)), UI.colorsEnabled)
        menu.addItem(colorsItem)
        loginItem = toggleItem("Open at Login", #selector(toggleOpenAtLogin(_:)), LoginItem.enabled)
        menu.addItem(loginItem)
        let settings = NSMenuItem(title: "Settings…", action: #selector(openSettings(_:)), keyEquivalent: ",")
        settings.target = self
        menu.addItem(settings)
        menu.addItem(.separator())
        if updater != nil {
            let check = NSMenuItem(title: "Check for Updates…", action: #selector(checkWithSparkle(_:)),
                                   keyEquivalent: "")
            check.target = self
            menu.addItem(check)
        }
        let crash = NSMenuItem(title: "Show Crash Reports…", action: #selector(showCrashReports(_:)),
                               keyEquivalent: "")
        crash.target = self
        crash.isHidden = CrashGuard.crashReports().isEmpty
        menu.addItem(crash)
        crashItem = crash
        menu.addItem(symbolItem("Restart", #selector(restartApp(_:)), "arrow.clockwise"))
        // Our own selector, not terminate: (macOS 26 decorates the stock item).
        menu.addItem(symbolItem("Quit", #selector(quitApp(_:)), "power"))
        menu.delegate = self
        statusItem.menu = menu
    }

    /// Keep the toggles in step with the Settings window.
    func menuWillOpen(_ menu: NSMenu) {
        glassItem.state = UI.liquidGlassEnabled ? .on : .off
        colorsItem.state = UI.colorsEnabled ? .on : .off
        loginItem.state = LoginItem.enabled ? .on : .off
        crashItem?.isHidden = CrashGuard.crashReports().isEmpty
    }

    @objc func openSettings(_ sender: Any?) { settingsWindow.show() }

    @objc func checkWithSparkle(_ sender: Any?) { updater?.checkForUpdates(sender) }

    /// Reveal macOS's crash reports for EMC in Finder (attach one to an issue).
    @objc func showCrashReports(_ sender: Any?) {
        let reports = CrashGuard.crashReports()
        if reports.isEmpty { return }
        NSWorkspace.shared.activateFileViewerSelecting(Array(reports.prefix(5)))
    }

    @objc func quitApp(_ sender: Any?) { NSApp.terminate(nil) }

    @objc func checkForUpdates(_ sender: Any?) {
        Updates.checkAsync { [weak self] version, url in self?.showUpdateAvailable(version, url) }
    }

    func showUpdateAvailable(_ version: String, _ url: String) {
        if version == Updates.ignoredVersion { return }
        updateURL = url
        updateVersion = version
        let title = "Update Available — \(version)"
        if let item = updateItem {
            item.title = title
            return
        }
        // Red dot beside the tray icon (an attributed title keeps its colour).
        if let button = statusItem.button {
            button.imagePosition = .imageLeft
            button.attributedTitle = NSAttributedString(string: " ●", attributes: [
                .foregroundColor: NSColor.systemRed, .font: NSFont.systemFont(ofSize: 8), .baselineOffset: 5,
            ])
        }
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        let view = NSMenuItem(title: "View Release Page", action: #selector(openReleasePage(_:)), keyEquivalent: "")
        view.target = self
        submenu.addItem(view)
        let ignore = NSMenuItem(title: "Ignore This Version", action: #selector(ignoreUpdate(_:)), keyEquivalent: "")
        ignore.target = self
        submenu.addItem(ignore)
        item.submenu = submenu
        let separator = NSMenuItem.separator()
        statusItem.menu?.insertItem(item, at: 0)
        statusItem.menu?.insertItem(separator, at: 1)
        updateItem = item
        updateSeparator = separator
    }

    @objc func openReleasePage(_ sender: Any?) {
        if let url = updateURL.flatMap(URL.init(string:)) { NSWorkspace.shared.open(url) }
    }

    /// Skip this release: drop the badge and menu entry and remember it.
    @objc func ignoreUpdate(_ sender: Any?) {
        if let version = updateVersion { Updates.ignoredVersion = version }
        if let item = updateItem { statusItem.menu?.removeItem(item) }
        if let sep = updateSeparator { statusItem.menu?.removeItem(sep) }
        updateItem = nil
        updateSeparator = nil
        statusItem.button?.title = ""
        statusItem.button?.imagePosition = .imageOnly
    }

    /// Relaunch: a detached shell waits for this process to exit, then starts
    /// a fresh copy (avoids two instances racing for the status item).
    @objc func restartApp(_ sender: Any?) {
        let pid = ProcessInfo.processInfo.processIdentifier
        let bundlePath = Bundle.main.bundlePath
        let relaunch = bundlePath.hasSuffix(".app")
            ? "/usr/bin/open -n '\(bundlePath.replacingOccurrences(of: "'", with: "'\\''"))'"
            : "'\(CommandLine.arguments[0].replacingOccurrences(of: "'", with: "'\\''"))'"
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = ["-c", "while /bin/kill -0 \(pid) 2>/dev/null; do sleep 0.2; done; exec \(relaunch)"]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
        NSApp.terminate(nil)
    }

    @objc func toggleLiquidGlass(_ sender: Any?) {
        UI.liquidGlassEnabled.toggle()
        glassItem.state = UI.liquidGlassEnabled ? .on : .off
        // Pooled views bake the style in at creation; rebuild them.
        missionControl.rebuildStyle()
    }

    @objc func toggleColors(_ sender: Any?) {
        UI.colorsEnabled.toggle()
        colorsItem.state = UI.colorsEnabled ? .on : .off
        missionControl.rebuildStyle()
    }

    @objc func toggleOpenAtLogin(_ sender: Any?) {
        loginItem.state = LoginItem.setEnabled(!LoginItem.enabled) ? .on : .off
    }
}
