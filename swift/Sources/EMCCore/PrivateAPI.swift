import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

/// Private (but long-stable) system calls, resolved at runtime with dlsym so a
/// symbol that disappears in a future macOS degrades to "unavailable" — the
/// same fallbacks the Python version took when ctypes couldn't find them —
/// instead of failing to launch.
enum PrivateAPI {
    private typealias GetWindowFn = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> AXError
    private typealias MainConnectionFn = @convention(c) () -> Int32
    private typealias GetActiveSpaceFn = @convention(c) (Int32) -> UInt64
    private typealias CopyManagedDisplaySpacesFn = @convention(c) (Int32) -> Unmanaged<CFArray>?
    private typealias CoreDockSendNotificationFn = @convention(c) (CFString, UnsafeMutableRawPointer?) -> Void

    private static let frameworkPaths = [
        "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework/HIServices",
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
        "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
    ]

    private static func symbol(_ name: String) -> UnsafeMutableRawPointer? {
        // RTLD_DEFAULT first: AppKit already loaded every framework we need.
        if let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) { return sym }
        for path in frameworkPaths {
            if let handle = dlopen(path, RTLD_LAZY), let sym = dlsym(handle, name) {
                return sym
            }
        }
        return nil
    }

    private static let getWindowPtr = symbol("_AXUIElementGetWindow")
    private static let mainConnectionPtr = symbol("CGSMainConnectionID")
    private static let activeSpacePtr = symbol("CGSGetActiveSpace")
    private static let managedSpacesPtr = symbol("CGSCopyManagedDisplaySpaces")
    private static let dockNotifyPtr = symbol("CoreDockSendNotification")

    /// Whether AX windows can be mapped to CG window numbers here.
    static var windowNumbersAvailable: Bool { getWindowPtr != nil }

    /// The CG window number of an AX window (the call window managers use).
    static func windowNumber(of element: AXUIElement) -> CGWindowID? {
        guard let ptr = getWindowPtr else { return nil }
        let fn = unsafeBitCast(ptr, to: GetWindowFn.self)
        var number: CGWindowID = 0
        return fn(element, &number) == .success ? number : nil
    }

    private static func connection() -> Int32? {
        guard let ptr = mainConnectionPtr else { return nil }
        return unsafeBitCast(ptr, to: MainConnectionFn.self)()
    }

    /// The managed id of the space currently shown on the focused display.
    static func activeSpaceID() -> Int? {
        guard let cid = connection(), let ptr = activeSpacePtr else { return nil }
        let id = unsafeBitCast(ptr, to: GetActiveSpaceFn.self)(cid)
        return id == 0 ? nil : Int(id)
    }

    /// CGSCopyManagedDisplaySpaces: one dictionary per display with its
    /// "Spaces" and "Current Space".
    static func managedDisplaySpaces() -> [[String: Any]]? {
        guard let cid = connection(), let ptr = managedSpacesPtr else { return nil }
        let fn = unsafeBitCast(ptr, to: CopyManagedDisplaySpacesFn.self)
        guard let array = fn(cid)?.takeRetainedValue() else { return nil }
        return array as? [[String: Any]]
    }

    /// Post a Dock notification (what the Show Desktop gesture and hot corner
    /// use). False if the symbol can't be found.
    static func coreDockNotify(_ name: String) -> Bool {
        guard let ptr = dockNotifyPtr else { return false }
        unsafeBitCast(ptr, to: CoreDockSendNotificationFn.self)(name as CFString, nil)
        return true
    }
}
