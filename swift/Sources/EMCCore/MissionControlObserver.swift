import AppKit
import ApplicationServices
import Foundation

/// Accessibility notifications from the processes that host Mission Control
/// (the Dock, and WindowManager on macOS 27). Which notifications they post as
/// Mission Control opens is undocumented, so this only ever ACCELERATES the
/// detect poll: every notification triggers an immediate check. Once
/// notifications have reliably preceded several openings, the controller
/// slows its idle poll; a single opening found without one restores it.
final class MissionControlObserver {
    static let notifications = [
        "AXCreated", "AXUIElementDestroyed", "AXWindowCreated", "AXLayoutChanged",
        "AXFocusedUIElementChanged", "AXFocusedWindowChanged", "AXValueChanged",
    ]
    static let hostBundles = [AXBridge.dockBundle, AXBridge.windowManagerBundle]

    private var observers: [pid_t: AXObserver] = [:]
    private let onEvent: () -> Void

    init(onEvent: @escaping () -> Void) {
        self.onEvent = onEvent
    }

    deinit { removeAll() }

    /// (Re)attach to the current Dock / WindowManager processes. Cheap when
    /// nothing changed; a respawned Dock gets a fresh observer.
    func refresh() {
        var live: Set<pid_t> = []
        for bundle in Self.hostBundles {
            guard let pid = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
                .first?.processIdentifier else { continue }
            live.insert(pid)
            if observers[pid] == nil { attach(pid) }
        }
        for pid in observers.keys where !live.contains(pid) { detach(pid) }
    }

    var attachedCount: Int { observers.count }

    private func attach(_ pid: pid_t) {
        var observer: AXObserver?
        let callback: AXObserverCallback = { _, _, _, refcon in
            guard let refcon = refcon else { return }
            Unmanaged<MissionControlObserver>.fromOpaque(refcon).takeUnretainedValue().onEvent()
        }
        guard AXObserverCreate(pid, callback, &observer) == .success, let obs = observer else { return }
        let app = AXUIElementCreateApplication(pid)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        var added = 0
        for name in Self.notifications
        where AXObserverAddNotification(obs, app, name as CFString, refcon) == .success {
            added += 1
        }
        guard added > 0 else { return }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
        observers[pid] = obs
        Log.geom("OBSERVER attached pid=\(pid) notifications=\(added)")
    }

    private func detach(_ pid: pid_t) {
        guard let obs = observers.removeValue(forKey: pid) else { return }
        CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
    }

    func removeAll() {
        for pid in Array(observers.keys) { detach(pid) }
    }
}
