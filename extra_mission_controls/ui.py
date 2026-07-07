"""Shared UI helpers."""

import objc
from AppKit import (
    NSAttributedString,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
)
from Foundation import NSMakeRect

CLOSE_BUTTON_SIZE = 24.0


class CloseButton(NSButton):
    """Round dark ✕ button that turns red on hover.

    Hover is driven by explicit setHovered_ calls — the Dock owns mouse
    events while Mission Control is up, so tracking areas never fire and
    the controller polls the cursor instead (the tracking area still works
    if the button ever lives in a normal window). Also accepts the first
    click in a non-key window, required for non-activating panels."""

    _hovered = objc.ivar()
    _label = objc.ivar()

    def acceptsFirstMouse_(self, event):
        return True

    def setLabel_(self, text):
        """The button's glyph: '✕' normally, '✕2' on a Split View tile whose
        single button closes both apps."""
        if self._label == text:
            return
        self._label = text
        self.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            text,
            {
                NSForegroundColorAttributeName: NSColor.whiteColor(),
                NSFontAttributeName: NSFont.boldSystemFontOfSize_(
                    12.0 if len(text) <= 1 else 9.0),
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
            # macOS traffic-light red.
            layer.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    1.0, 0.37, 0.34, 0.95).CGColor())
            layer.setBorderColor_(
                NSColor.whiteColor().colorWithAlphaComponent_(0.9).CGColor())
        else:
            layer.setBackgroundColor_(
                NSColor.blackColor().colorWithAlphaComponent_(0.7).CGColor())
            layer.setBorderColor_(
                NSColor.whiteColor().colorWithAlphaComponent_(0.4).CGColor())


def make_close_button(target, action, size=CLOSE_BUTTON_SIZE):
    """A CloseButton wired to target/action. Caller positions it."""
    button = CloseButton.buttonWithTitle_target_action_("", target, action)
    button.setBordered_(False)
    button.setLabel_("✕")
    button.setFrame_(NSMakeRect(0, 0, size, size))
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(size / 2)
    button.layer().setBorderWidth_(1.0)
    button.setHovered_(False)  # applies the base style
    return button
