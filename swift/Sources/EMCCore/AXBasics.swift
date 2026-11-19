import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

// Low-level Accessibility and WindowServer helpers shared by AXBridge.

enum AX {
    /// True when this process may use the Accessibility API.
    static func isTrusted(prompt: Bool = false) -> Bool {
        if prompt {
            let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            return AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        }
        return AXIsProcessTrusted()
    }

    static func element(_ value: AnyObject?) -> AXUIElement? {
        guard let value = value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }

    static func attribute(_ element: AXUIElement?, _ name: String) -> AnyObject? {
        guard let element = element else { return nil }
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(element, name as CFString, &value)
        return err == .success ? value : nil
    }

    static func string(_ element: AXUIElement?, _ name: String) -> String? {
        attribute(element, name) as? String
    }

    /// AXEnabled etc.: true/false, or nil when unreadable (callers treat nil
    /// like Python's None — "not known to be false").
    static func bool(_ element: AXUIElement?, _ name: String) -> Bool? {
        guard let value = attribute(element, name) else { return nil }
        if CFGetTypeID(value) == CFBooleanGetTypeID() {
            return CFBooleanGetValue((value as! CFBoolean))
        }
        return (value as? NSNumber)?.boolValue
    }

    static func child(_ element: AXUIElement?, _ name: String) -> AXUIElement? {
        AX.element(attribute(element, name))
    }

    static func children(_ element: AXUIElement?) -> [AXUIElement] {
        elements(attribute(element, "AXChildren"))
    }

    static func elements(_ value: AnyObject?) -> [AXUIElement] {
        guard let array = value as? [AnyObject] else { return [] }
        return array.compactMap { AX.element($0) }
    }

    static func point(_ value: AnyObject?) -> CGPoint? {
        guard let value = value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        let axValue = value as! AXValue
        var point = CGPoint.zero
        guard AXValueGetType(axValue) == .cgPoint, AXValueGetValue(axValue, .cgPoint, &point) else {
            return nil
        }
        return point
    }

    static func size(_ value: AnyObject?) -> CGSize? {
        guard let value = value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
        let axValue = value as! AXValue
        var size = CGSize.zero
        guard AXValueGetType(axValue) == .cgSize, AXValueGetValue(axValue, .cgSize, &size) else {
            return nil
        }
        return size
    }

    /// Read several attributes with ONE AX round-trip instead of one Mach IPC
    /// per attribute. Missing attributes come back as error-typed AXValues,
    /// which the typed readers above turn into nil.
    static func multi(_ element: AXUIElement, _ names: [String]) -> [String: AnyObject] {
        var values: CFArray?
        let err = AXUIElementCopyMultipleAttributeValues(
            element, names as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &values)
        if err == .success, let array = values as? [AnyObject], array.count == names.count {
            var out: [String: AnyObject] = [:]
            for (name, value) in zip(names, array) { out[name] = value }
            return out
        }
        var out: [String: AnyObject] = [:]
        for name in names {
            if let value = attribute(element, name) { out[name] = value }
        }
        return out
    }

    @discardableResult
    static func press(_ element: AXUIElement?) -> Bool {
        guard let element = element else { return false }
        return AXUIElementPerformAction(element, "AXPress" as CFString) == .success
    }

    @discardableResult
    static func perform(_ element: AXUIElement?, _ action: String) -> Bool {
        guard let element = element else { return false }
        return AXUIElementPerformAction(element, action as CFString) == .success
    }

    @discardableResult
    static func set(_ element: AXUIElement?, _ name: String, _ value: AnyObject) -> AXError {
        guard let element = element else { return .failure }
        return AXUIElementSetAttributeValue(element, name as CFString, value)
    }

    static func settable(_ element: AXUIElement, _ name: String) -> Bool? {
        var settable: DarwinBoolean = false
        let err = AXUIElementIsAttributeSettable(element, name as CFString, &settable)
        return err == .success ? settable.boolValue : nil
    }

    static func pid(of element: AXUIElement) -> pid_t? {
        var pid: pid_t = 0
        return AXUIElementGetPid(element, &pid) == .success && pid != 0 ? pid : nil
    }

    static func app(_ pid: pid_t) -> AXUIElement {
        let app = AXUIElementCreateApplication(pid)
        // Don't let one unresponsive app stall us for the default 6 seconds.
        AXUIElementSetMessagingTimeout(app, 0.5)
        return app
    }

    static func windows(of pid: pid_t) -> [AXUIElement] {
        elements(attribute(app(pid), "AXWindows"))
    }

    static func same(_ a: AXUIElement?, _ b: AXUIElement?) -> Bool {
        guard let a = a, let b = b else { return false }
        return CFEqual(a, b)
    }

    /// True while the AX element still refers to an existing UI object.
    static func alive(_ element: AXUIElement?) -> Bool {
        element != nil && attribute(element, "AXRole") != nil
    }

    static func frame(of window: AXUIElement?) -> Rect? {
        guard let window = window,
              let p = point(attribute(window, "AXPosition")),
              let s = size(attribute(window, "AXSize")) else { return nil }
        return Rect(Double(p.x), Double(p.y), Double(s.width), Double(s.height))
    }

    static func actionNames(_ element: AXUIElement) -> [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
        return (names as? [String]) ?? []
    }

    static func macOSMajor() -> Int {
        let major = ProcessInfo.processInfo.operatingSystemVersion.majorVersion
        // A binary linked against an old SDK is shown the 10.16/16.0
        // compatibility version: "can't tell".
        return (major == 10 || major == 16) ? 0 : major
    }

    static let isMacOS27 = macOSMajor() >= 27
}

/// One entry of a CGWindowList query.
struct CGWin {
    var number: Int
    var pid: pid_t
    var owner: String
    var name: String
    var layer: Int
    var alpha: Double
    var bounds: CGRect?

    init(_ entry: [String: Any]) {
        number = (entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0
        pid = (entry[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? 0
        owner = entry[kCGWindowOwnerName as String] as? String ?? ""
        name = entry[kCGWindowName as String] as? String ?? ""
        layer = (entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        alpha = (entry[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1
        if let dict = entry[kCGWindowBounds as String] as? NSDictionary {
            bounds = CGRect(dictionaryRepresentation: dict as CFDictionary)
        }
    }

    var width: Double { Double(bounds?.width ?? 0) }
    var height: Double { Double(bounds?.height ?? 0) }

    var sig: WindowSig {
        let b = bounds ?? .zero
        return WindowSig(number: number, x: Int(b.origin.x.rounded()), y: Int(b.origin.y.rounded()),
                         w: Int(b.width.rounded()), h: Int(b.height.rounded()))
    }
}

enum WindowList {
    static func onScreen() -> [CGWin] {
        query([.optionOnScreenOnly, .excludeDesktopElements])
    }

    static func query(_ options: CGWindowListOption, _ relativeTo: CGWindowID = kCGNullWindowID) -> [CGWin] {
        guard let raw = CGWindowListCopyWindowInfo(options, relativeTo) as? [[String: Any]] else {
            return []
        }
        return raw.map(CGWin.init)
    }

    /// Descriptions of SPECIFIC windows by id (~0.1ms for a handful versus
    /// ~1ms for a full list pass). The array holds raw window ids cast to
    /// pointers, as CGWindowListCreateDescriptionFromArray expects.
    static func describe(_ ids: [Int]) -> [CGWin] {
        let ids = ids.filter { $0 > 0 }
        guard !ids.isEmpty else { return [] }
        var pointers: [UnsafeRawPointer?] = ids.map { UnsafeRawPointer(bitPattern: UInt($0)) }
        guard let array = CFArrayCreate(nil, &pointers, pointers.count, nil),
              let raw = CGWindowListCreateDescriptionFromArray(array) as? [[String: Any]] else {
            return []
        }
        return raw.map(CGWin.init)
    }
}

/// Snapshot of one normal on-screen window (windows.py's WindowInfo).
struct WindowInfo: Equatable {
    var windowID: Int
    var pid: pid_t
    var owner: String
    var title: String
    var x: Double
    var y: Double
    var width: Double
    var height: Double
}

enum Windows {
    /// System processes that never own closable user windows.
    static let skipOwners: Set<String> = [
        "Dock", "Window Server", "WindowManager", "Control Center", "Control Centre",
        "Notification Center", "Notification Centre", "Spotlight", "Wallpaper",
        "screencapture", "TextInputMenuAgent",
    ]
    /// Smaller than this (points) is a palette/tooltip, not a real window.
    static let minDimension = 80.0

    /// Every normal window on the current space (or all spaces), front to back.
    static func list(excludePid: pid_t? = nil, allSpaces: Bool = false) -> [WindowInfo] {
        let options: CGWindowListOption = allSpaces
            ? [.optionAll, .excludeDesktopElements]
            : [.optionOnScreenOnly, .excludeDesktopElements]
        var out: [WindowInfo] = []
        for win in WindowList.query(options) {
            if win.layer != 0 { continue }
            if let excludePid = excludePid, win.pid == excludePid { continue }
            if skipOwners.contains(win.owner) { continue }
            guard let b = win.bounds, Double(b.width) >= minDimension,
                  Double(b.height) >= minDimension else { continue }
            out.append(WindowInfo(windowID: win.number, pid: win.pid, owner: win.owner,
                                  title: win.name, x: Double(b.origin.x), y: Double(b.origin.y),
                                  width: Double(b.width), height: Double(b.height)))
        }
        return out
    }
}
