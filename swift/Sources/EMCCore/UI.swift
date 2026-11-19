import AppKit

/// Shared UI constants, settings, and view factories (a port of ui.py).
enum UI {
    /// The round button's diameter (Settings → Button size; 24 by default).
    static var closeButtonSize: CGFloat { CGFloat(Settings.buttonSize) }

    // Hover fills echo the traffic lights: close red, minimize yellow, full
    // screen green; quit purple, cancel (↺) blue, arrange (◫) teal.
    static let hoverRed = RGB(1.0, 0.37, 0.34)
    static let hoverYellow = RGB(1.0, 0.74, 0.18)
    static let hoverGreen = RGB(0.22, 0.74, 0.32)
    static let hoverPurple = RGB(0.66, 0.42, 0.94)
    static let hoverBlue = RGB(0.30, 0.62, 0.98)
    static let hoverTeal = RGB(0.16, 0.72, 0.70)

    /// Glyphs whose drawing sits small in its em-box → drawn larger/heavier.
    static let symbolGlyphs: Set<String> = ["⤢", "❏", "−", "⏻", "⏼", "⭘", "◐", "↺", "◫"]
    static let glyphPoints: [String: CGFloat] = ["⤢": 28, "◫": 21]

    // Flyout menu geometry, shared by the quit, arrange and Dock menus.
    static let menuWidth: CGFloat = 158
    static let menuRowHeight: CGFloat = 28
    static let menuPad: CGFloat = 6
    static let menuCaptionHeight: CGFloat = 28
    static let captionFontSize: CGFloat = 13

    typealias MenuItem = (glyph: String, label: String, rgb: RGB)

    static let quitMenuItems: [MenuItem] = [
        ("⏻", "Quit", hoverPurple), ("⏼", "Force Quit", hoverRed), ("⭘", "Force Kill", hoverRed),
    ]
    static let arrangeMenuItems: [MenuItem] = [
        ("◧", "Left Half", hoverTeal), ("◨", "Right Half", hoverTeal), ("■", "Maximize", hoverTeal),
    ]
    static let dockMenuItems: [MenuItem] = [
        ("✕", "Close All Windows", hoverRed), ("⏻", "Quit", hoverPurple), ("⏼", "Force Quit", hoverRed),
    ]

    static func menuHeight(_ count: Int, caption: String? = nil) -> CGFloat {
        menuPad * 2 + menuRowHeight * CGFloat(count) + ((caption?.isEmpty == false) ? menuCaptionHeight : 0)
    }

    static func captionWidth(_ text: String?) -> CGFloat {
        NSAttributedString(string: text ?? "",
                           attributes: [.font: NSFont.systemFont(ofSize: captionFontSize)]).size().width
    }

    // -- settings ---------------------------------------------------------------

    static let glassKey = "EMCLiquidGlass"
    static let colorsKey = "EMCButtonColors"

    /// Liquid Glass overlays (macOS 26+). Default ON (unset counts as on).
    static var liquidGlassEnabled: Bool {
        get {
            UserDefaults.standard.object(forKey: glassKey) == nil
                ? true : UserDefaults.standard.bool(forKey: glassKey)
        }
        set { UserDefaults.standard.set(newValue, forKey: glassKey) }
    }

    /// Traffic-light hues on the buttons. Default OFF (monochrome).
    static var colorsEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: colorsKey) }
        set { UserDefaults.standard.set(newValue, forKey: colorsKey) }
    }

    static func color(_ rgb: RGB, _ alpha: CGFloat) -> NSColor {
        NSColor(calibratedRed: CGFloat(rgb.r), green: CGFloat(rgb.g), blue: CGFloat(rgb.b), alpha: alpha)
    }

    // -- glyph images -------------------------------------------------------------

    private static var glyphCache: [String: NSImage] = [:]

    /// A size×size white image of `text`, optically centred by its INK bounds
    /// so glyphs that draw small and off-centre (⤢, ◫) sit right. Labels
    /// "sf:<name>[@<degrees>]|<fallback>" draw an SF Symbol instead.
    static func glyphImage(_ text: String, point: CGFloat, size: CGFloat = closeButtonSize) -> NSImage {
        let key = "\(text)|\(point)|\(size)"
        if let cached = glyphCache[key] { return cached }
        let image = renderCenteredGlyph(text, point: point, size: size)
        glyphCache[key] = image
        return image
    }

    private static func symbolImage(_ name: String, point: CGFloat, size: CGFloat, degrees: CGFloat) -> NSImage? {
        guard let base = NSImage(systemSymbolName: name, accessibilityDescription: nil) else { return nil }
        let config = NSImage.SymbolConfiguration(pointSize: point, weight: .bold)
        let symbol = base.withSymbolConfiguration(config) ?? base
        var w = symbol.size.width, h = symbol.size.height
        let scale = min(1, (size - 6) / max(w, h, 1))
        w *= scale; h *= scale
        let rect = NSRect(x: (size - w) / 2, y: (size - h) / 2, width: w, height: h)
        let image = NSImage(size: NSSize(width: size, height: size))
        image.lockFocus()
        if degrees != 0 {
            let turn = NSAffineTransform()
            turn.translateX(by: size / 2, yBy: size / 2)
            turn.rotate(byDegrees: degrees)
            turn.translateX(by: -size / 2, yBy: -size / 2)
            turn.concat()
        }
        symbol.draw(in: rect)
        NSColor.white.set()
        rect.fill(using: .sourceAtop)
        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    private static func renderCenteredGlyph(_ input: String, point: CGFloat, size: CGFloat) -> NSImage {
        var text = input
        if text.hasPrefix("sf:") {
            let spec = text.dropFirst(3)
            let parts = spec.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
            let nameParts = parts[0].split(separator: "@", maxSplits: 1)
            let name = String(nameParts.first ?? "")
            let degrees = nameParts.count > 1 ? CGFloat(Double(nameParts[1]) ?? 0) : 0
            if let image = symbolImage(name, point: point, size: size, degrees: degrees) { return image }
            text = parts.count > 1 && !parts[1].isEmpty ? String(parts[1]) : "?"
        }
        let glyph = NSAttributedString(string: text, attributes: [
            .foregroundColor: NSColor.white,
            .font: NSFont.systemFont(ofSize: point, weight: .heavy),
        ])
        let image = NSImage(size: NSSize(width: size, height: size))
        // Pass 1: draw at a known pen point into an 8-bit bitmap and scan its
        // alpha bytes for the ink box.
        let canvas = Int(size * 2)
        let pen = size * 0.5
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: canvas, pixelsHigh: canvas,
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                         isPlanar: false, colorSpaceName: .calibratedRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0),
              let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return image }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        glyph.draw(at: NSPoint(x: pen, y: pen))
        NSGraphicsContext.restoreGraphicsState()
        guard let data = rep.bitmapData else { return image }
        let bpr = rep.bytesPerRow, spp = rep.samplesPerPixel
        let aoff = rep.bitmapFormat.contains(.alphaFirst) ? 0 : spp - 1
        var minx = Int.max, miny = Int.max, maxx = -1, maxy = -1
        for py in 0..<canvas {
            let row = py * bpr
            for px in 0..<canvas where data[row + px * spp + aoff] > 50 {
                minx = min(minx, px); maxx = max(maxx, px)
                miny = min(miny, py); maxy = max(maxy, py)
            }
        }
        guard maxx >= 0 else { return image }
        let inkCX = CGFloat(minx + maxx + 1) / 2
        let inkCY = CGFloat(canvas) - CGFloat(miny + maxy + 1) / 2   // pixel y is top-down
        // Pass 2: draw so the ink centre lands at the image centre.
        image.lockFocus()
        glyph.draw(at: NSPoint(x: pen + size / 2 - inkCX, y: pen + size / 2 - inkCY))
        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    /// A stretchable rounded-rect mask — the supported way to round an
    /// NSVisualEffectView's corners.
    static func roundedMaskImage(_ radius: CGFloat) -> NSImage {
        let size = radius * 2 + 2
        let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            NSColor.white.set()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }

    static func hudBlur(_ frame: NSRect, _ radius: CGFloat) -> NSVisualEffectView {
        let blur = NSVisualEffectView(frame: frame)
        blur.blendingMode = .behindWindow
        blur.material = .hudWindow
        blur.state = .active  // panels are never key
        blur.maskImage = roundedMaskImage(radius)
        return blur
    }

    // -- factories --------------------------------------------------------------

    /// A CloseButton wired to target/action, wrapped in a Liquid Glass disc
    /// where available. Add `outerView` to the panel.
    static func makeCloseButton(target: AnyObject?, action: Selector?, size: CGFloat = closeButtonSize) -> CloseButton {
        let button = CloseButton(frame: NSRect(x: 0, y: 0, width: size, height: size))
        button.title = ""
        button.target = target
        button.action = action
        button.isBordered = false
        button.wantsLayer = true
        button.setHoverRGB(hoverRed)
        button.setLabel("✕")
        if let glass = Glass.make(NSRect(x: 0, y: 0, width: size, height: size), cornerRadius: size / 2) {
            Glass.setContent(glass, button)  // the glyph rides on top of the glass
            button.glass = glass
        }
        // A bright rim keeps the disc's edge visible over flat backgrounds.
        button.layer?.cornerRadius = size / 2
        button.layer?.borderWidth = 1
        button.applyStyle()
        return button
    }

    static func makeMarkOverlay(_ frame: NSRect) -> MarkView {
        let view = MarkView(frame: frame)
        view.wantsLayer = true
        return view
    }

    /// The flyout's views: `root` goes in the panel (Liquid Glass, else a dark
    /// HUD blur), `menu` is driven with setItems / setHighlight.
    static func makeMenu(_ frame: NSRect) -> (root: NSView, menu: MenuView) {
        let view = MenuView(frame: frame)
        view.wantsLayer = true
        view.layer?.cornerRadius = 10
        view.layer?.borderWidth = 1
        view.layer?.borderColor = NSColor.white.withAlphaComponent(0.28).cgColor
        if let glass = Glass.make(frame, cornerRadius: 10) {
            Glass.setContent(glass, view)
            return (glass, view)
        }
        let blur = hudBlur(frame, 10)
        view.autoresizingMask = [.width, .height]
        blur.addSubview(view)
        return (blur, view)
    }

    /// The tray behind a tile's row of buttons.
    static func makeTray(_ frame: NSRect, cornerRadius: CGFloat) -> NSView {
        if let glass = Glass.make(frame, cornerRadius: cornerRadius) {
            glass.wantsLayer = true
            glass.layer?.cornerRadius = cornerRadius
            glass.layer?.borderWidth = 1
            glass.layer?.borderColor = NSColor.white.withAlphaComponent(0.22).cgColor
            return glass
        }
        let blur = hudBlur(frame, cornerRadius)
        // The rim rides a transparent subview (a border on the blur's own
        // layer would ignore the mask's rounded shape).
        let rim = NSView(frame: NSRect(origin: .zero, size: frame.size))
        rim.autoresizingMask = [.width, .height]
        rim.wantsLayer = true
        rim.layer?.cornerRadius = cornerRadius
        rim.layer?.borderWidth = 1
        rim.layer?.borderColor = NSColor.white.withAlphaComponent(0.2).cgColor
        blur.addSubview(rim)
        return blur
    }
}

/// Liquid Glass (NSGlassEffectView, macOS 26+), compiled only with an SDK
/// that has it and used only where it runs; otherwise every caller falls back
/// to the flat/blur style.
enum Glass {
    static func make(_ frame: NSRect, cornerRadius: CGFloat) -> NSView? {
        guard UI.liquidGlassEnabled else { return nil }
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView(frame: frame)
            glass.cornerRadius = cornerRadius
            return glass
        }
        #endif
        return nil
    }

    static func setContent(_ glass: NSView, _ content: NSView) {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *), let glass = glass as? NSGlassEffectView {
            glass.contentView = content
            return
        }
        #endif
        glass.addSubview(content)
    }

    static func setCornerRadius(_ glass: NSView, _ radius: CGFloat) {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *), let glass = glass as? NSGlassEffectView {
            glass.cornerRadius = radius
        }
        #endif
    }

    static func setTint(_ glass: NSView, _ color: NSColor?) {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *), let glass = glass as? NSGlassEffectView {
            glass.tintColor = color
        }
        #endif
    }
}

/// Round overlay button: a glyph on a Liquid Glass disc (or a translucent dark
/// disc) that highlights on hover. Hover is driven by explicit setHovered
/// calls — the Dock owns mouse events while Mission Control is up, so the
/// controller polls the cursor instead.
final class CloseButton: NSButton {
    private var hovered: Bool?
    private var label: String?
    private var hoverRGB: RGB?
    var glass: NSView?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    /// The view to place in the panel: the glass wrapper when present.
    var outerView: NSView { glass ?? self }

    func setHoverRGB(_ rgb: RGB) {
        guard hoverRGB != rgb else { return }
        hoverRGB = rgb
        applyStyle()
    }

    /// The button's glyph, drawn as an ink-centred image.
    func setLabel(_ text: String) {
        guard label != text else { return }
        label = text
        let point: CGFloat
        if text.hasPrefix("sf:") {
            point = 13
        } else if let p = UI.glyphPoints[text] {
            point = p
        } else if UI.symbolGlyphs.contains(text) {
            point = 17
        } else {
            point = text.count <= 1 ? 12 : 9
        }
        // Glyph sizes are tuned for the default 24pt disc; scale with it.
        let size = UI.closeButtonSize
        imagePosition = .imageOnly
        imageScaling = .scaleProportionallyDown
        image = UI.glyphImage(text, point: point * size / 24, size: size)
    }

    func setHovered(_ value: Bool) {
        guard hovered != value else { return }
        hovered = value
        applyStyle()
    }

    /// Resize the round button (and its glass wrapper), keeping it a circle.
    func setDiameter(_ size: CGFloat) {
        let rect = NSRect(x: 0, y: 0, width: size, height: size)
        frame = rect
        if let glass = glass {
            glass.frame = rect
            Glass.setCornerRadius(glass, size / 2)
        }
        layer?.cornerRadius = size / 2
    }

    /// Fill + rim for the current style (glass vs flat), colour setting and
    /// hover state. Glass is tinted with the material's own tintColor and
    /// only a whisper of paint; flat keeps the solid discs.
    func applyStyle() {
        let isGlass = glass != nil
        let isHovered = hovered ?? false
        let rgb = hoverRGB ?? UI.hoverRed
        var tint: NSColor?
        var fill: (RGB, CGFloat)
        var rim: CGFloat
        if UI.colorsEnabled {
            if isGlass {
                tint = UI.color(rgb, 1)
                fill = (rgb, isHovered ? 0.62 : 0.26)
                rim = isHovered ? 0.65 : 0.38
            } else if isHovered {
                fill = (rgb, 0.95); rim = 0.9
            } else {
                fill = (rgb, 0.55); rim = 0.45
            }
        } else if isGlass {
            fill = isHovered ? (RGB(0.42, 0.42, 0.42), 0.45) : (RGB(0, 0, 0), 0.22)
            rim = isHovered ? 0.75 : 0.35
        } else if isHovered {
            fill = (RGB(0.30, 0.30, 0.30), 0.92); rim = 0.95
        } else {
            fill = (RGB(0, 0, 0), 0.7); rim = 0.4
        }
        if let glass = glass { Glass.setTint(glass, tint) }
        guard let layer = layer else { return }
        layer.backgroundColor = UI.color(fill.0, fill.1).cgColor
        layer.borderColor = NSColor.white.withAlphaComponent(rim).cgColor
    }
}

/// A dim rounded scrim with a big centred glyph over a thumbnail marked for a
/// deferred action. Purely decorative: its panel ignores the mouse.
final class MarkView: NSView {
    private var glyph: String?

    func setGlyph(_ value: String) {
        guard glyph != value else { return }
        glyph = value
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let w = bounds.width, h = bounds.height
        guard w > 0, h > 0 else { return }
        let radius = min(12, min(w, h) * 0.18)
        NSColor.black.withAlphaComponent(0.45).set()
        NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill()
        let point = max(18, min(w, h) * 0.4)
        let text = NSAttributedString(string: glyph ?? "✕", attributes: [
            .foregroundColor: NSColor.white.withAlphaComponent(0.92),
            .font: NSFont.systemFont(ofSize: point, weight: .heavy),
        ])
        let size = text.size()
        text.draw(at: NSPoint(x: (w - size.width) / 2, y: (h - size.height) / 2))
    }
}

/// The dark pop-up flyout (quit / arrange / Dock menus), drawn as our own view
/// because a native menu never sees clicks while the Dock owns the mouse.
final class MenuView: NSView {
    private var highlight = -1
    private var items: [UI.MenuItem] = []
    private var caption: String?

    override var isFlipped: Bool { true }  // row 0 at the top

    func setItems(_ items: [UI.MenuItem], caption: String? = nil) {
        self.items = items
        self.caption = caption
        needsDisplay = true
    }

    func setHighlight(_ index: Int) {
        guard highlight != index else { return }
        highlight = index
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let width = bounds.width
        for (index, item) in items.enumerated() {
            let rowY = UI.menuPad + CGFloat(index) * UI.menuRowHeight
            if index == highlight {
                if UI.colorsEnabled {
                    UI.color(item.rgb, 0.95).set()
                } else {
                    NSColor(calibratedWhite: 0.42, alpha: 0.85).set()
                }
                NSBezierPath(roundedRect: NSRect(x: 4, y: rowY, width: width - 8, height: UI.menuRowHeight),
                             xRadius: 5, yRadius: 5).fill()
            }
            let glyph = NSAttributedString(string: item.glyph, attributes: [
                .foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 15, weight: .heavy),
            ])
            let gsize = glyph.size()
            glyph.draw(at: NSPoint(x: 10 + (24 - gsize.width) / 2, y: rowY + (UI.menuRowHeight - gsize.height) / 2))
            let label = NSAttributedString(string: item.label, attributes: [
                .foregroundColor: NSColor.white.withAlphaComponent(0.95), .font: NSFont.systemFont(ofSize: 13),
            ])
            let lsize = label.size()
            label.draw(at: NSPoint(x: 42, y: rowY + (UI.menuRowHeight - lsize.height) / 2))
        }
        if let caption = caption, !caption.isEmpty {
            // Footer caption in the style of the Dock's name bubble.
            let capY = UI.menuPad + UI.menuRowHeight * CGFloat(items.count)
            NSColor.white.withAlphaComponent(0.14).set()
            NSRect(x: 10, y: capY + 1, width: width - 20, height: 1).fill()
            let text = NSAttributedString(string: caption, attributes: [
                .foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: UI.captionFontSize),
            ])
            let csize = text.size()
            text.draw(at: NSPoint(x: (width - csize.width) / 2,
                                  y: capY + 2 + (UI.menuCaptionHeight - 2 - csize.height) / 2))
        }
    }
}
