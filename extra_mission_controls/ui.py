"""Shared UI helpers."""

import objc
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightHeavy,
    NSForegroundColorAttributeName,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
)

from Foundation import NSMakePoint, NSMakeRect

CLOSE_BUTTON_SIZE = 24.0

# Hover fills, echoing the macOS traffic lights: close → red, minimize →
# yellow, full screen / exit full screen (the zoom button) → green.
HOVER_RED = (1.0, 0.37, 0.34)
HOVER_YELLOW = (1.0, 0.74, 0.18)
HOVER_GREEN = (0.22, 0.74, 0.32)

# Glyphs whose drawing sits small inside its em-box (arrows/box/dash look tiny
# next to ✕ at the same point size) → drawn larger and heavier so they stay
# legible in the same circle.
_SYMBOL_GLYPHS = ("⤢", "❏", "−")


class CloseButton(NSButton):
    """Round dark overlay button (a glyph over a translucent disc) that
    highlights on hover.

    Hover is driven by explicit setHovered_ calls — the Dock owns mouse
    events while Mission Control is up, so tracking areas never fire and
    the controller polls the cursor instead (the tracking area still works
    if the button ever lives in a normal window). Also accepts the first
    click in a non-key window, required for non-activating panels."""

    _hovered = objc.ivar()
    _label = objc.ivar()
    _hover_rgb = objc.ivar()

    def acceptsFirstMouse_(self, event):
        return True

    def setHoverRGB_(self, rgb):
        if self._hover_rgb != rgb:
            self._hover_rgb = rgb
            self.applyStyle()

    def setLabel_(self, text):
        """The button's glyph: '✕' close, '✕2' close-both, '❏' windowed,
        '⤢' full screen. The symbol glyphs are drawn larger and heavier so
        they read as clearly as the ✕ within the same-size circle."""
        if self._label == text:
            return
        self._label = text
        if text in _SYMBOL_GLYPHS:
            font = NSFont.systemFontOfSize_weight_(17.0, NSFontWeightHeavy)
        else:
            font = NSFont.boldSystemFontOfSize_(12.0 if len(text) <= 1 else 9.0)
        self.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            text,
            {
                NSForegroundColorAttributeName: NSColor.whiteColor(),
                NSFontAttributeName: font,
            }))

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

    def applyStyle(self):
        layer = self.layer()
        if layer is None:
            return
        if self._hovered:
            r, g, b = self._hover_rgb or HOVER_RED
            layer.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    r, g, b, 0.95).CGColor())
            layer.setBorderColor_(
                NSColor.whiteColor().colorWithAlphaComponent_(0.9).CGColor())
        else:
            layer.setBackgroundColor_(
                NSColor.blackColor().colorWithAlphaComponent_(0.7).CGColor())
            layer.setBorderColor_(
                NSColor.whiteColor().colorWithAlphaComponent_(0.4).CGColor())


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


def make_close_button(target, action, size=CLOSE_BUTTON_SIZE):
    """A CloseButton wired to target/action. Caller positions it, sets its
    glyph with setLabel_ and its hover fill with setHoverRGB_."""
    button = CloseButton.buttonWithTitle_target_action_("", target, action)
    button.setBordered_(False)
    button.setHoverRGB_(HOVER_RED)
    button.setLabel_("✕")
    button.setFrame_(NSMakeRect(0, 0, size, size))
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(size / 2)
    button.layer().setBorderWidth_(1.0)
    button.setHovered_(False)  # applies the base style
    return button
