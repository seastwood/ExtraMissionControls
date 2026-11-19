import Foundation

/// User preferences beyond the style toggles in UI (Liquid Glass, Colors).
/// Everything defaults to the behaviour of the Python app.
enum Settings {
    private static var defaults: UserDefaults { .standard }

    private static func bool(_ key: String, default value: Bool) -> Bool {
        defaults.object(forKey: key) == nil ? value : defaults.bool(forKey: key)
    }

    // -- which buttons appear ---------------------------------------------------

    /// The thumbnail/desktop buttons a user can hide. Cancel (↺) and the
    /// Spaces Bar tile buttons always show.
    static let optionalButtons: [ButtonKind] = [.window, .minimize, .makeFullscreen, .arrange, .quit, .desktop]

    static func shows(_ kind: ButtonKind) -> Bool {
        guard optionalButtons.contains(kind) else { return true }
        return bool("EMCShow.\(kind.rawValue)", default: true)
    }

    static func setShows(_ kind: ButtonKind, _ on: Bool) {
        defaults.set(on, forKey: "EMCShow.\(kind.rawValue)")
    }

    static var dockMenuEnabled: Bool {
        get { bool("EMCDockMenu", default: true) }
        set { defaults.set(newValue, forKey: "EMCDockMenu") }
    }

    static var middleClickCloses: Bool {
        get { bool("EMCMiddleClickClose", default: true) }
        set { defaults.set(newValue, forKey: "EMCMiddleClickClose") }
    }

    // -- size -----------------------------------------------------------------------

    static let buttonSizeRange: ClosedRange<Double> = 18...32
    static let defaultButtonSize = 24.0

    static var buttonSize: Double {
        get {
            let value = defaults.double(forKey: "EMCButtonSize")
            return value == 0 ? defaultButtonSize
                : min(max(value, buttonSizeRange.lowerBound), buttonSizeRange.upperBound)
        }
        set { defaults.set(newValue, forKey: "EMCButtonSize") }
    }

    // -- keyboard shortcuts over a thumbnail -----------------------------------------

    static let shortcutActions: [WindowAction] = [.close, .minimize, .quit]
    static let defaultShortcutLetters: [WindowAction: String] = [.close: "W", .minimize: "M", .quit: "Q"]

    static var shortcutsEnabled: Bool {
        get { bool("EMCShortcuts", default: true) }
        set { defaults.set(newValue, forKey: "EMCShortcuts") }
    }

    static func shortcutLetter(_ action: WindowAction) -> String {
        let stored = defaults.string(forKey: "EMCShortcut.\(action.rawValue)")?.uppercased()
        if let stored = stored, letterKeycodes[stored] != nil { return stored }
        return defaultShortcutLetters[action] ?? ""
    }

    static func setShortcutLetter(_ action: WindowAction, _ letter: String) {
        defaults.set(letter.uppercased(), forKey: "EMCShortcut.\(action.rawValue)")
    }

    /// The action bound to an ANSI key code, if shortcuts are on.
    static func shortcutAction(forKeycode code: Int64) -> WindowAction? {
        guard shortcutsEnabled else { return nil }
        return shortcutActions.first { letterKeycodes[shortcutLetter($0)] == code }
    }

    /// ANSI virtual key codes for the letters (layout-independent positions,
    /// as the Python app used).
    static let letterKeycodes: [String: Int64] = [
        "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11,
        "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "O": 31, "U": 32, "I": 34, "P": 35,
        "L": 37, "J": 38, "K": 40, "N": 45, "M": 46,
    ]
    static let letters = letterKeycodes.keys.sorted()
}
