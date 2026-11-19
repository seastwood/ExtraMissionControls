import Foundation
import ServiceManagement

/// Open at Login. The native app registers itself with SMAppService (listed
/// under System Settings → General → Login Items). The Python version used a
/// per-user LaunchAgent plist; that file is cleaned up here so an upgrade
/// never starts two copies at login.
enum LoginItem {
    static let legacyLabel = "com.seth.extramissioncontrols"
    static var legacyPlist: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(legacyLabel).plist")
    }

    static var enabled: Bool {
        SMAppService.mainApp.status == .enabled
            || FileManager.default.fileExists(atPath: legacyPlist.path)
    }

    /// Register or unregister. Returns the new state.
    @discardableResult
    static func setEnabled(_ on: Bool) -> Bool {
        removeLegacyAgent()
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            Log.error("open-at-login", "\(on ? "register" : "unregister") failed: \(error)")
        }
        return enabled
    }

    /// Carry a Python-era LaunchAgent over to SMAppService (once), so the
    /// setting survives the upgrade without a duplicate launch.
    static func migrateLegacyAgent() {
        guard FileManager.default.fileExists(atPath: legacyPlist.path) else { return }
        removeLegacyAgent()
        try? SMAppService.mainApp.register()
    }

    private static func removeLegacyAgent() {
        let path = legacyPlist.path
        guard FileManager.default.fileExists(atPath: path) else { return }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["bootout", "gui/\(getuid())", path]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
        task.waitUntilExit()
        try? FileManager.default.removeItem(atPath: path)
    }
}
