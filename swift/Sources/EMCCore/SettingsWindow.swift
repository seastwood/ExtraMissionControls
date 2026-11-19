import AppKit
import SwiftUI

/// Backing model for the Settings window: each change is saved immediately
/// and `onStyleChange` rebuilds the pooled overlays so it shows the next time
/// Mission Control opens.
final class SettingsModel: ObservableObject {
    var onStyleChange: () -> Void = {}

    @Published var liquidGlass = UI.liquidGlassEnabled {
        didSet { guard liquidGlass != oldValue else { return }; UI.liquidGlassEnabled = liquidGlass; onStyleChange() }
    }
    @Published var colors = UI.colorsEnabled {
        didSet { guard colors != oldValue else { return }; UI.colorsEnabled = colors; onStyleChange() }
    }
    @Published var buttonSize = Settings.buttonSize {
        didSet { guard buttonSize != oldValue else { return }; Settings.buttonSize = buttonSize; onStyleChange() }
    }
    @Published var shown: [ButtonKind: Bool] = Dictionary(
        uniqueKeysWithValues: Settings.optionalButtons.map { ($0, Settings.shows($0)) }) {
        didSet { for (kind, on) in shown { Settings.setShows(kind, on) } }
    }
    @Published var dockMenu = Settings.dockMenuEnabled {
        didSet { Settings.dockMenuEnabled = dockMenu }
    }
    @Published var middleClick = Settings.middleClickCloses {
        didSet { Settings.middleClickCloses = middleClick }
    }
    @Published var shortcuts = Settings.shortcutsEnabled {
        didSet { Settings.shortcutsEnabled = shortcuts }
    }
    @Published var letters: [WindowAction: String] = Dictionary(
        uniqueKeysWithValues: Settings.shortcutActions.map { ($0, Settings.shortcutLetter($0)) }) {
        didSet { for (action, letter) in letters { Settings.setShortcutLetter(action, letter) } }
    }
    @Published var openAtLogin = LoginItem.enabled {
        didSet {
            guard openAtLogin != oldValue else { return }
            let actual = LoginItem.setEnabled(openAtLogin)
            if actual != openAtLogin { openAtLogin = actual }
        }
    }

    /// Re-read values another surface (the menu bar) may have changed.
    func reload() {
        liquidGlass = UI.liquidGlassEnabled
        colors = UI.colorsEnabled
        openAtLogin = LoginItem.enabled
    }

    func binding(_ kind: ButtonKind) -> Binding<Bool> {
        Binding(get: { self.shown[kind] ?? true }, set: { self.shown[kind] = $0 })
    }

    func letter(_ action: WindowAction) -> Binding<String> {
        Binding(get: { self.letters[action] ?? "" }, set: { self.letters[action] = $0 })
    }

    var duplicateLetters: Bool { Set(letters.values).count < letters.count }
}

struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    static let buttonNames: [ButtonKind: String] = [
        .window: "✕  Close", .minimize: "−  Minimize", .makeFullscreen: "⤢  Full screen",
        .arrange: "◫  Arrange", .quit: "⏻  Quit", .desktop: "Show Desktop (bottom right)",
    ]
    static let actionNames: [WindowAction: String] = [.close: "Close", .minimize: "Minimize", .quit: "Quit app"]

    var body: some View {
        Form {
            Section(header: Text("Buttons")) {
                ForEach(Settings.optionalButtons, id: \.rawValue) { kind in
                    Toggle(Self.buttonNames[kind] ?? kind.rawValue, isOn: model.binding(kind))
                }
                Toggle("Dock menu (hover a running app's icon)", isOn: $model.dockMenu)
                Toggle("Middle-click a thumbnail to close it", isOn: $model.middleClick)
            }
            Section(header: Text("Appearance")) {
                Toggle("Liquid Glass", isOn: $model.liquidGlass)
                Toggle("Traffic-light colors", isOn: $model.colors)
                HStack {
                    Text("Button size")
                    Slider(value: $model.buttonSize, in: Settings.buttonSizeRange, step: 1)
                    Text("\(Int(model.buttonSize)) pt").monospacedDigit().frame(width: 44, alignment: .trailing)
                }
            }
            Section(header: Text("Keyboard shortcuts over a thumbnail")) {
                Toggle("Enabled", isOn: $model.shortcuts)
                ForEach(Settings.shortcutActions, id: \.rawValue) { action in
                    Picker(Self.actionNames[action] ?? action.rawValue, selection: model.letter(action)) {
                        ForEach(Settings.letters, id: \.self) { Text($0).tag($0) }
                    }
                    .disabled(!model.shortcuts)
                }
                if model.duplicateLetters {
                    Text("Two actions share a key; the first one listed wins.")
                        .font(.footnote).foregroundColor(.orange)
                }
            }
            Section(header: Text("General")) {
                Toggle("Open at Login", isOn: $model.openAtLogin)
            }
            Text("Changes apply the next time you open Mission Control.")
                .font(.footnote).foregroundColor(.secondary)
        }
        .padding(20)
        .frame(width: 420)
    }
}

final class SettingsWindowController: NSObject, NSWindowDelegate {
    let model = SettingsModel()
    private var window: NSWindow?

    func show() {
        model.reload()
        if window == nil {
            let hosting = NSHostingController(rootView: SettingsView(model: model))
            let win = NSWindow(contentViewController: hosting)
            win.title = "Extra Mission Controls Settings"
            win.styleMask = [.titled, .closable]
            win.isReleasedWhenClosed = false
            win.delegate = self
            win.center()
            window = win
        }
        // A menu-bar (accessory) app must activate to bring its window forward.
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
