import Foundation

/// Version shown in the menu and compared against GitHub releases. The built
/// app's Info.plist wins when present (scripts/build_app.sh stamps it).
public enum EMCVersion {
    public static let fallback = "0.11.0"
    public static var current: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String)
            ?? fallback
    }
}

/// Logging. Errors always go to ~/Library/Logs/ExtraMissionControls.log (the
/// built app has no Terminal, and a silent failure looked like EMC "dying").
/// Everything else is debug-only (EMC_DEBUG=1) and returns immediately in
/// normal use: no string building, no disk writes.
enum Log {
    static let debugEnabled = ProcessInfo.processInfo.environment["EMC_DEBUG"] != nil

    static let logsDir: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        return dir
    }()

    static let errorLog = logsDir.appendingPathComponent("ExtraMissionControls.log")
    /// Debug traces live together in one folder (the Python version wrote
    /// them next to the source tree, which a .app doesn't have).
    static let debugDir = logsDir.appendingPathComponent("ExtraMissionControls", isDirectory: true)
    static let geometryLog: URL = {
        if let path = ProcessInfo.processInfo.environment["EMC_GEOMETRY_LOG"] {
            return URL(fileURLWithPath: path)
        }
        return debugDir.appendingPathComponent("emc_trace.log")
    }()
    static let actionLog = debugDir.appendingPathComponent("emc_actions.log")
    static let diagFile = debugDir.appendingPathComponent("emc_diag.txt")

    static let actionPrefixes = ["UNFULLSCREEN", "CLOSE-FULLSCREEN", "APPLY-DEFERRED",
                                 "NATIVE-DROP", "MAKE-FULLSCREEN", "PANELS", "ARRANGE",
                                 "CLICK", "DOCK", "DESKTOP", "PIN", "registry"]

    static func prepareDebugLogs() {
        guard debugEnabled else { return }
        try? FileManager.default.createDirectory(at: debugDir, withIntermediateDirectories: true)
        try? "Extra Mission Controls geometry log\n".write(to: geometryLog, atomically: true,
                                                           encoding: .utf8)
    }

    static func append(_ text: String, to url: URL, rotateAt limit: Int? = nil) {
        let fm = FileManager.default
        if let limit = limit,
           let size = (try? fm.attributesOfItem(atPath: url.path)[.size]) as? Int,
           size > limit {
            let old = url.appendingPathExtension("old")
            try? fm.removeItem(at: old)
            try? fm.moveItem(at: url, to: old)
        }
        guard let data = (text.hasSuffix("\n") ? text : text + "\n").data(using: .utf8) else {
            return
        }
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url)
        }
    }

    /// Console + trace file, debug runs only.
    static func debug(_ message: @autoclosure () -> String) {
        guard debugEnabled else { return }
        let text = message()
        print("EMC:", text)
        append(text, to: geometryLog)
    }

    /// Diagnostic trace (close chains, layouts) — debug runs only. Actions are
    /// mirrored to emc_actions.log.
    static func geom(_ message: @autoclosure () -> String) {
        guard debugEnabled else { return }
        let text = message()
        if actionPrefixes.contains(where: { text.hasPrefix($0) }) {
            let stamp = DateFormatter.localizedString(from: Date(), dateStyle: .none,
                                                      timeStyle: .medium)
            append("\(stamp) \(text)", to: actionLog, rotateAt: 200_000)
        }
        append(text, to: geometryLog)
    }

    /// Always on: a short line in the error log (rare, so it costs nothing).
    static func error(_ where: String, _ detail: String = "") {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let text = "\(stamp) \(`where`) \(detail)"
        print(text)
        try? FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)
        append(text, to: errorLog, rotateAt: 512 * 1024)
    }

    /// User-visible notes the Python app printed to stdout.
    static func note(_ message: String) {
        print("Extra Mission Controls: \(message)")
    }
}

/// Wall-clock and monotonic time, mirroring time.time() / time.monotonic().
enum Clock {
    static func now() -> Double { Date().timeIntervalSince1970 }
    static func monotonic() -> Double { ProcessInfo.processInfo.systemUptime }
}
