"""Menu-bar app shell: status item, permission prompts, and the run loop."""

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import __version__, ax, ui
from .mission_control import MissionControlButtons


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notification):
        self.mission_control = MissionControlButtons.alloc().init()
        self.mission_control.start()
        self._setup_status_item()

        ax_ok, capture_ok = ax.ensure_permissions()
        if not ax_ok:
            print("ExtraMissionControls: Accessibility permission missing — "
                  "nothing will work until it is granted "
                  "(System Settings → Privacy & Security → Accessibility).")
        if not capture_ok:
            print("ExtraMissionControls: Screen Recording permission missing — "
                  "window-title matching is degraded, so closing apps with "
                  "broken accessibility (e.g. Steam) may fail "
                  "(System Settings → Privacy & Security → Screen Recording).")

    def _setup_status_item(self):
        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        button = self._status_item.button()
        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "rectangle.grid.2x2", "ExtraMissionControls")
        if symbol is not None:
            button.setImage_(symbol)
        else:
            button.setTitle_("⧉")

        menu = NSMenu.alloc().init()
        info = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Extra Mission Controls %s" % __version__, None, "")
        info.setEnabled_(False)
        menu.addItem_(info)
        menu.addItem_(NSMenuItem.separatorItem())
        # Liquid Glass is off by default (vibrancy is the most GPU-expensive
        # part of the overlay); this checkbox turns it on for looks.
        glass = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Liquid Glass", "toggleLiquidGlass:", "")
        glass.setTarget_(self)
        glass.setState_(NSControlStateValueOn if ui.liquid_glass_enabled()
                        else NSControlStateValueOff)
        menu.addItem_(glass)
        self._glass_item = glass
        # Traffic-light colours for the buttons (both styles); off = neutral.
        colors = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Colors", "toggleColors:", "")
        colors.setTarget_(self)
        colors.setState_(NSControlStateValueOn if ui.colors_enabled()
                         else NSControlStateValueOff)
        menu.addItem_(colors)
        self._colors_item = colors
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit ExtraMissionControls", "terminate:", "q"))
        self._status_item.setMenu_(menu)

    def toggleLiquidGlass_(self, sender):
        ui.set_liquid_glass(not ui.liquid_glass_enabled())
        self._glass_item.setState_(
            NSControlStateValueOn if ui.liquid_glass_enabled()
            else NSControlStateValueOff)
        # Pooled views bake the style in at creation; rebuild them so the
        # change shows the next time Mission Control opens.
        self.mission_control.rebuild_style()

    def toggleColors_(self, sender):
        ui.set_colors(not ui.colors_enabled())
        self._colors_item.setState_(
            NSControlStateValueOn if ui.colors_enabled()
            else NSControlStateValueOff)
        self.mission_control.rebuild_style()


_delegate = None  # NSApplication.delegate is weak; keep a strong reference


def main():
    global _delegate
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _delegate = AppDelegate.alloc().init()
    app.setDelegate_(_delegate)
    AppHelper.runEventLoop()
