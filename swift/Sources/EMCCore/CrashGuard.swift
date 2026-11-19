import AppKit
import Darwin
import Foundation

// Everything the signal handler touches is prepared up front, so the handler
// itself only spawns the relaunch shell (posix_spawn is a single system call
// on Darwin; Swift doesn't offer fork) and re-raises the signal.
private var relaunchArgv: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?
private var shellPath: UnsafeMutablePointer<CChar>?

private func emcCrashHandler(_ sig: Int32) {
    if let argv = relaunchArgv, let sh = shellPath {
        var child: pid_t = 0
        _ = posix_spawn(&child, sh, nil, nil, argv, environ)
    }
    // Let macOS write its usual crash report, then die as before.
    signal(sig, SIG_DFL)
    raise(sig)
}

/// The Python app caught every error and carried on; a Swift crash ends the
/// process. CrashGuard relaunches the app after a crash — at most 3 times in
/// 2 minutes, so a crash loop stops — and points the menu at the report
/// macOS wrote.
enum CrashGuard {
    static let fatalSignals: [Int32] = [SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT, SIGTRAP]
    static let crashLog = Log.logsDir.appendingPathComponent("ExtraMissionControls/crashes.txt")
    static let maxCrashes = 3
    static let crashWindow = 120

    /// Install the relaunch handler. Only for the built .app: a crash while
    /// running from the terminal should just stop.
    static func install() {
        let bundle = Bundle.main.bundlePath
        guard bundle.hasSuffix(".app") else { return }
        try? FileManager.default.createDirectory(at: crashLog.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        let quote = { (s: String) in "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        // Record the crash; relaunch unless this is a crash loop.
        let script = """
        f=\(quote(crashLog.path)); now=$(/bin/date +%s); echo "$now" >> "$f"; \
        n=$(/usr/bin/awk -v t=$((now-\(crashWindow))) '$1>t' "$f" | /usr/bin/wc -l); \
        [ "$n" -le \(maxCrashes) ] && /bin/sleep 1 && exec /usr/bin/open -n \(quote(bundle))
        """
        let args = ["/bin/sh", "-c", script]
        let argv = UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>.allocate(capacity: args.count + 1)
        for (i, arg) in args.enumerated() { argv[i] = strdup(arg) }
        argv[args.count] = nil
        relaunchArgv = argv
        shellPath = strdup("/bin/sh")
        for sig in fatalSignals { signal(sig, emcCrashHandler) }
        NSSetUncaughtExceptionHandler { exception in
            Log.error("uncaught exception", "\(exception.name.rawValue): \(exception.reason ?? "")")
        }
    }

    /// Crash reports macOS wrote for this app, newest first.
    static func crashReports() -> [URL] {
        let dir = Log.logsDir.appendingPathComponent("DiagnosticReports")
        let files = (try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        func date(_ url: URL) -> Date {
            (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
        }
        return files.filter { $0.lastPathComponent.hasPrefix("ExtraMissionControls") }
            .sorted { date($0) > date($1) }
    }

    /// True when the last launch ended in a crash (a crash recorded within
    /// the last minute — this launch is the relaunch).
    static func relaunchedAfterCrash() -> Bool {
        guard let text = try? String(contentsOf: crashLog, encoding: .utf8),
              let last = text.split(separator: "\n").last, let stamp = Double(last) else { return false }
        return Date().timeIntervalSince1970 - stamp < 60
    }
}
