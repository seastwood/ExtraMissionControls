"""Shared UI helpers."""

import AppKit
import objc
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageRep,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightHeavy,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSImageOnly,
    NSImageScaleProportionallyDown,
    NSCalibratedRGBColorSpace,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
)

from Foundation import NSMakePoint, NSMakeRect, NSMakeSize

CLOSE_BUTTON_SIZE = 24.0

# Hover fills, echoing the macOS traffic lights: close → red, minimize →
# yellow, full screen / exit full screen (the zoom button) → green. Off the
# traffic-light palette to set them apart: quit → purple, cancel a pending
# action (↺) → blue, and arrange/tile a window (◫) → teal.
HOVER_RED = (1.0, 0.37, 0.34)
HOVER_YELLOW = (1.0, 0.74, 0.18)
HOVER_GREEN = (0.22, 0.74, 0.32)
HOVER_PURPLE = (0.66, 0.42, 0.94)
HOVER_BLUE = (0.30, 0.62, 0.98)
HOVER_TEAL = (0.16, 0.72, 0.70)

# Glyphs whose drawing sits small inside its em-box (arrows/box/dash/power/undo/
# split look tiny next to ✕ at the same point size) → drawn larger and heavier
# so they stay legible in the same circle.
_SYMBOL_GLYPHS = ("⤢", "❏", "−", "⏻", "↺", "◫")
# Per-glyph point-size overrides: ⤢ and ◫ draw especially small even at the
# symbol size, so bump them to match the other symbols' ink (~12-13 px at 24).
_GLYPH_POINTS = {"⤢": 28.0, "◫": 21.0}

# Flyout menu geometry, shared by the quit and arrange menus. Each menu is a
# list of (glyph, label, hover rgb) rows, top to bottom; the controller maps
# each row index to an action (see _QUIT_MENU_ACTIONS / _ARRANGE_MENU_ACTIONS)
# in the same order.
MENU_WIDTH = 158.0
MENU_ROW_HEIGHT = 28.0
MENU_PAD = 6.0
# Quit button (right-click / hover-dwell): quit, then two escalations.
QUIT_MENU_ITEMS = (
    ("⏻", "Quit", HOVER_PURPLE),
    ("☠️", "Force Quit", HOVER_RED),
    ("💀", "Force Kill", HOVER_RED),
)
# Arrange button (◫, click): fill the left/right half of the window's screen,
# or maximize it to the whole desktop (not macOS full screen).
ARRANGE_MENU_ITEMS = (
    ("◧", "Left Half", HOVER_TEAL),
    ("◨", "Right Half", HOVER_TEAL),
    ("■", "Maximize", HOVER_TEAL),
)


def menu_height(items):
    """Total height of a flyout menu panel for the given rows."""
    return MENU_PAD * 2 + MENU_ROW_HEIGHT * len(items)


_glyph_image_cache = {}


def _glyph_image(text, point, size=CLOSE_BUTTON_SIZE):
    """A `size`×`size` white image of `text`, optically centred by its *ink*
    bounds (not the font box the button would centre) so glyphs that draw small
    and off-centre — ⤢, ◫ — sit right. Cached per glyph/point/size, so the
    two-pass render below runs once per distinct glyph."""
    key = (text, point, size)
    image = _glyph_image_cache.get(key)
    if image is None:
        image = _render_centered_glyph(text, point, size)
        _glyph_image_cache[key] = image
    return image


def _render_centered_glyph(text, point, size):
    font = NSFont.systemFontOfSize_weight_(point, NSFontWeightHeavy)
    attrs = {NSForegroundColorAttributeName: NSColor.whiteColor(),
             NSFontAttributeName: font}
    glyph = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    # Pass 1: draw at a known pen point into an 8-bit bitmap (a roomy 1x canvas)
    # and scan its raw alpha bytes for the ink box. Same y-up coordinate space as
    # NSImage.lockFocus below, so the centring maths carries over; scanning bytes
    # (vs colorAtX:y:) keeps this well under a millisecond per glyph.
    canvas, pen = int(size * 2), size * 0.5
    rep = NSBitmapImageRep.alloc().\
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, canvas, canvas, 8, 4, True, False,
            NSCalibratedRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    glyph.drawAtPoint_(NSMakePoint(pen, pen))
    NSGraphicsContext.restoreGraphicsState()
    data = rep.bitmapData()
    bpr, spp = int(rep.bytesPerRow()), int(rep.samplesPerPixel())
    aoff = 0 if (int(rep.bitmapFormat()) & 1) else spp - 1   # alpha-first?
    minx = miny = 1 << 30
    maxx = maxy = -1
    for py in range(canvas):
        row = py * bpr
        for px in range(canvas):
            if data[row + px * spp + aoff] > 50:
                if px < minx:
                    minx = px
                if px > maxx:
                    maxx = px
                if py < miny:
                    miny = py
                if py > maxy:
                    maxy = py
    if maxx < 0:  # nothing drawn (unexpected) — return the blank image
        return image
    ink_cx = (minx + maxx + 1) / 2.0
    ink_cy = canvas - (miny + maxy + 1) / 2.0   # pixel y is top-down → flip
    # Pass 2: draw so the ink centre lands at the image centre.
    image.lockFocus()
    glyph.drawAtPoint_(NSMakePoint(pen + size / 2.0 - ink_cx,
                                   pen + size / 2.0 - ink_cy))
    image.unlockFocus()
    image.setTemplate_(False)  # render as-drawn (white), not tinted by the button
    return image


_GLASS_KEY = "EMCLiquidGlass"
_liquid_glass = None  # cached setting; loaded from user defaults on first use


def liquid_glass_enabled():
    """Whether the overlays use Liquid Glass (NSGlassEffectView). Default OFF:
    vibrancy is the most GPU-expensive thing the overlay draws, so the flat
    style is the energy-efficient default; the menu-bar dropdown toggles it
    (persisted in user defaults)."""
    global _liquid_glass
    if _liquid_glass is None:
        from Foundation import NSUserDefaults
        _liquid_glass = bool(
            NSUserDefaults.standardUserDefaults().boolForKey_(_GLASS_KEY))
    return _liquid_glass


def set_liquid_glass(enabled):
    """Persist the Liquid Glass preference. Callers must rebuild any pooled
    views afterwards — the setting is read at view creation time."""
    global _liquid_glass
    _liquid_glass = bool(enabled)
    from Foundation import NSUserDefaults
    NSUserDefaults.standardUserDefaults().setBool_forKey_(
        _liquid_glass, _GLASS_KEY)


_COLORS_KEY = "EMCButtonColors"
_button_colors = None  # cached setting; loaded from user defaults on first use


def colors_enabled():
    """Whether the round buttons carry their traffic-light hues (close red,
    minimize yellow, zoom green, …). Default ON — the colours echo the macOS
    traffic lights and make each button's job obvious; the menu-bar dropdown
    can switch to a neutral dark disc. Applies to both the Liquid Glass and
    the flat styles (applyStyle reads this each time it runs). Unset in user
    defaults counts as ON, so boolForKey_ (which is False when unset) is only
    trusted once the key actually exists."""
    global _button_colors
    if _button_colors is None:
        from Foundation import NSUserDefaults
        defaults = NSUserDefaults.standardUserDefaults()
        if defaults.objectForKey_(_COLORS_KEY) is None:
            _button_colors = True
        else:
            _button_colors = bool(defaults.boolForKey_(_COLORS_KEY))
    return _button_colors


def set_colors(enabled):
    """Persist the button-colour preference. Callers rebuild the pooled buttons
    afterwards so the new style is applied."""
    global _button_colors
    _button_colors = bool(enabled)
    from Foundation import NSUserDefaults
    NSUserDefaults.standardUserDefaults().setBool_forKey_(
        _button_colors, _COLORS_KEY)


def _make_glass(frame, corner_radius):
    """An NSGlassEffectView (Liquid Glass, macOS 26+) sized to `frame` with the
    given corner radius, or None when disabled (the energy-saving default,
    toggleable from the menu bar) or where the class is unavailable (older
    macOS) —
    callers then fall back to a flat translucent layer, so the app looks the
    same as before there."""
    if not liquid_glass_enabled():
        return None
    cls = getattr(AppKit, "NSGlassEffectView", None)
    if cls is None:
        return None
    glass = cls.alloc().initWithFrame_(frame)
    glass.setCornerRadius_(corner_radius)
    return glass


class CloseButton(NSButton):
    """Round overlay button — a glyph on a Liquid Glass disc (macOS 26+), or a
    translucent dark disc on older systems — that highlights on hover.

    Hover is driven by explicit setHovered_ calls — the Dock owns mouse
    events while Mission Control is up, so tracking areas never fire and
    the controller polls the cursor instead (the tracking area still works
    if the button ever lives in a normal window). Also accepts the first
    click in a non-key window, required for non-activating panels."""

    _hovered = objc.ivar()
    _label = objc.ivar()
    _hover_rgb = objc.ivar()
    _glass = objc.ivar()

    def acceptsFirstMouse_(self, event):
        return True

    def setHoverRGB_(self, rgb):
        if self._hover_rgb != rgb:
            self._hover_rgb = rgb
            self.applyStyle()

    def setLabel_(self, text):
        """The button's glyph: '✕' close, '✕2' close-both, '❏' windowed,
        '⤢' full screen, '↺' cancel, '◫' arrange. Drawn as an ink-centred image
        (not the button's own title) so glyphs that sit small and off-centre in
        their font box — ⤢, ◫ — are optically centred and sized to match ✕."""
        if self._label == text:
            return
        self._label = text
        if text in _GLYPH_POINTS:
            point = _GLYPH_POINTS[text]
        elif text in _SYMBOL_GLYPHS:
            point = 17.0
        else:
            point = 12.0 if len(text) <= 1 else 9.0
        self.setImagePosition_(NSImageOnly)
        self.setImageScaling_(NSImageScaleProportionallyDown)
        self.setImage_(_glyph_image(text, point))

    def updateTrackingAreas(self):
        objc.super(CloseButton, self).updateTrackingAreas()
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
            | NSTrackingInVisibleRect,
            self, None))

    def mouseEntered_(self, event):
        self.setHovered_(True)

    def mouseExited_(self, event):
        self.setHovered_(False)

    def setHovered_(self, hovered):
        hovered = bool(hovered)
        if self._hovered == hovered:
            return
        self._hovered = hovered
        self.applyStyle()

    def setGlass_(self, glass):
        self._glass = glass

    @objc.python_method
    def outerView(self):
        """The view to place in the panel: the glass wrapper when present, else
        the button itself (which draws its own disc)."""
        return self._glass if self._glass is not None else self

    def setDiameter_(self, size):
        """Resize the round button (and its glass wrapper) to `size`, keeping it
        a circle."""
        rect = NSMakeRect(0, 0, size, size)
        self.setFrame_(rect)
        if self._glass is not None:
            self._glass.setFrame_(rect)
            self._glass.setCornerRadius_(size / 2.0)
            self.layer().setCornerRadius_(size / 2.0)  # keep the rim circular
        else:
            self.layer().setCornerRadius_(size / 2.0)

    def applyStyle(self):
        # Fill + rim for the disc, computed for the current render style
        # (Liquid Glass vs flat), colour setting, and hover state.
        #
        # LIQUID GLASS aims to look like GLASS, not paint: the hue (when
        # Colors is on) is applied with NSGlassEffectView's own tintColor,
        # which infuses the material without covering it, and the paint layer
        # on top drops to a whisper — just enough dark/hue backing to keep
        # the white glyph legible over bright thumbnails. Hover brightens the
        # backing and the rim rather than flooding the disc. (tintColor
        # ignores its alpha — it sets a hue, not an opacity — which is
        # exactly what makes it the right vehicle for a glassy colour.)
        #
        # FLAT keeps the original solid discs: colour = hue tint at idle and
        # near-solid on hover; neutral = dark disc, bright rim on hover.
        glass = self._glass is not None
        hovered = self._hovered
        r, g, b = self._hover_rgb or HOVER_RED
        tint = None
        if colors_enabled():
            if glass:
                tint = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    r, g, b, 1.0)
                fill = (r, g, b, 0.62 if hovered else 0.26)
                rim = 0.65 if hovered else 0.38
            elif hovered:
                fill, rim = (r, g, b, 0.95), 0.9
            else:
                fill, rim = (r, g, b, 0.55), 0.45
        else:
            if glass:
                fill = ((0.42, 0.42, 0.42, 0.45) if hovered
                        else (0.0, 0.0, 0.0, 0.22))
                rim = 0.75 if hovered else 0.35
            elif hovered:
                fill, rim = (0.30, 0.30, 0.30, 0.92), 0.95
            else:
                fill, rim = (0.0, 0.0, 0.0, 0.7), 0.4
        if glass:
            self._glass.setTintColor_(tint)  # None clears to plain glass
        layer = self.layer()
        if layer is None:
            return
        fr, fg, fb, fa = fill
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                fr, fg, fb, fa).CGColor())
        layer.setBorderColor_(
            NSColor.whiteColor().colorWithAlphaComponent_(rim).CGColor())


class MarkView(NSView):
    """A dim rounded scrim with a big centred glyph, laid over a thumbnail
    that's been marked for a deferred close ('✕') or minimize ('−'). Subtle
    but unmistakable — 'this window will act when you leave Mission Control'.
    Purely decorative: the hosting panel ignores mouse events so clicks (to
    pan or dismiss) pass straight through."""

    _glyph = objc.ivar()

    def setGlyph_(self, glyph):
        if self._glyph != glyph:
            self._glyph = glyph
            self.setNeedsDisplay_(True)

    def drawRect_(self, dirty):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        if w <= 0 or h <= 0:
            return
        radius = min(12.0, min(w, h) * 0.18)
        scrim = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius)
        NSColor.blackColor().colorWithAlphaComponent_(0.45).set()
        scrim.fill()

        glyph = self._glyph or "✕"
        point = max(18.0, min(w, h) * 0.4)
        attrs = {
            NSForegroundColorAttributeName:
                NSColor.whiteColor().colorWithAlphaComponent_(0.92),
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(
                point, NSFontWeightHeavy),
        }
        text = NSAttributedString.alloc().initWithString_attributes_(glyph, attrs)
        tsize = text.size()
        text.drawAtPoint_(NSMakePoint((w - tsize.width) / 2.0,
                                      (h - tsize.height) / 2.0))


def make_mark_overlay(frame):
    """A layer-backed MarkView filling `frame`; caller sets its glyph with
    setGlyph_ and reparents/positions it via its hosting panel."""
    view = MarkView.alloc().initWithFrame_(frame)
    view.setWantsLayer_(True)
    return view


class MenuView(NSView):
    """The little dark pop-up flyout — used for both the quit menu (Quit /
    Force Quit / Force Kill) and the arrange menu (Left Half / Right Half).
    Its rows are supplied per open via set_items.

    Drawn as our own view rather than an NSMenu because the Dock owns the
    event stream while Mission Control is up — a native menu would never see
    the clicks. The controller positions the hosting panel, hit-tests the rows
    through the event tap, and drives the hovered row with set_highlight from
    its cursor poll (exactly how the buttons work)."""

    _highlight = objc.ivar()
    _items = objc.ivar()

    def isFlipped(self):
        return True  # row 0 at the top, so row math counts downward

    @objc.python_method
    def set_items(self, items):
        self._items = list(items)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_highlight(self, index):
        index = int(index)
        current = self._highlight
        current = int(current) if current is not None else None
        if current == index:
            return
        self._highlight = index
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty):
        bounds = self.bounds()
        width = bounds.size.width
        try:
            highlight = int(self._highlight)
        except (TypeError, ValueError):
            highlight = -1
        for index, (glyph, label, rgb) in enumerate(self._items or ()):
            row_y = MENU_PAD + index * MENU_ROW_HEIGHT
            if index == highlight:
                r, g, b = rgb
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    r, g, b, 0.95).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(4.0, row_y, width - 8.0, MENU_ROW_HEIGHT),
                    5.0, 5.0).fill()
            glyph_text = NSAttributedString.alloc().initWithString_attributes_(
                glyph, {
                    NSForegroundColorAttributeName: NSColor.whiteColor(),
                    NSFontAttributeName: NSFont.systemFontOfSize_weight_(
                        15.0, NSFontWeightHeavy),
                })
            gsize = glyph_text.size()
            glyph_text.drawAtPoint_(NSMakePoint(
                10.0 + (24.0 - gsize.width) / 2.0,
                row_y + (MENU_ROW_HEIGHT - gsize.height) / 2.0))
            label_text = NSAttributedString.alloc().initWithString_attributes_(
                label, {
                    NSForegroundColorAttributeName:
                        NSColor.whiteColor().colorWithAlphaComponent_(0.95),
                    NSFontAttributeName: NSFont.systemFontOfSize_(13.0),
                })
            lsize = label_text.size()
            label_text.drawAtPoint_(NSMakePoint(
                42.0, row_y + (MENU_ROW_HEIGHT - lsize.height) / 2.0))


def make_menu(frame):
    """Build the flyout's views. Returns (root, menu_view): `root` is placed in
    the panel — a Liquid Glass wrapper on macOS 26+, else the MenuView itself
    with a dark rounded layer — and `menu_view` is the MenuView the controller
    drives with set_items / set_highlight."""
    view = MenuView.alloc().initWithFrame_(frame)
    view.setWantsLayer_(True)
    view.set_items(())
    view.set_highlight(-1)
    glass = _make_glass(frame, 10.0)
    if glass is not None:
        glass.setContentView_(view)   # menu rows draw on top of the glass
        # Rim on the rows layer (on top of the glass) so the menu's edge reads
        # against a flat background, same idea as the buttons.
        layer = view.layer()
        layer.setCornerRadius_(10.0)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(
            NSColor.whiteColor().colorWithAlphaComponent_(0.28).CGColor())
        return glass, view
    layer = view.layer()
    layer.setCornerRadius_(10.0)
    layer.setMasksToBounds_(True)
    layer.setBackgroundColor_(
        NSColor.blackColor().colorWithAlphaComponent_(0.9).CGColor())
    layer.setBorderWidth_(1.0)
    layer.setBorderColor_(
        NSColor.whiteColor().colorWithAlphaComponent_(0.25).CGColor())
    return view, view


def make_close_button(target, action, size=CLOSE_BUTTON_SIZE):
    """A CloseButton wired to target/action, wrapped in a Liquid Glass disc
    where available (macOS 26+). The caller adds button.outerView() to its
    panel, sets the glyph with setLabel_ and the hover fill with setHoverRGB_."""
    button = CloseButton.buttonWithTitle_target_action_("", target, action)
    button.setBordered_(False)
    button.setHoverRGB_(HOVER_RED)
    button.setLabel_("✕")
    button.setFrame_(NSMakeRect(0, 0, size, size))
    button.setWantsLayer_(True)
    glass = _make_glass(NSMakeRect(0, 0, size, size), size / 2.0)
    if glass is not None:
        glass.setContentView_(button)   # the glyph rides on top of the glass
        button.setGlass_(glass)
        # A bright rim on the glyph layer (which sits on top of the glass) keeps
        # the disc's edge visible even over a flat, low-contrast background,
        # where the glass has nothing to refract. Its colour is set by
        # applyStyle (brighter on hover, for a bit of shine).
        button.layer().setCornerRadius_(size / 2.0)
        button.layer().setBorderWidth_(1.0)
    else:
        button.layer().setCornerRadius_(size / 2)
        button.layer().setBorderWidth_(1.0)
    button.setHovered_(False)  # applies the base style (glass tint or disc)
    return button


def make_tray(frame, corner_radius):
    """A Liquid Glass tray to sit behind a tile's row of buttons (macOS 26+),
    with a faint rim so its edge reads against a flat background; or a subtle
    translucent rounded view where glass is unavailable. Returned view is used
    as the tray panel's content view."""
    glass = _make_glass(frame, corner_radius)
    if glass is not None:
        glass.setWantsLayer_(True)
        layer = glass.layer()
        if layer is not None:
            layer.setCornerRadius_(corner_radius)
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(
                NSColor.whiteColor().colorWithAlphaComponent_(0.22).CGColor())
        return glass
    view = NSView.alloc().initWithFrame_(frame)
    view.setWantsLayer_(True)
    layer = view.layer()
    layer.setCornerRadius_(corner_radius)
    layer.setBackgroundColor_(
        NSColor.blackColor().colorWithAlphaComponent_(0.4).CGColor())
    return view
