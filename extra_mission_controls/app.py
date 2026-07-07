"""Menu-bar app shell: status item, permission prompts, and the run loop."""

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import ax
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
            "✕ buttons active in Mission Control", None, "")
        info.setEnabled_(False)
        menu.addItem_(info)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit ExtraMissionControls", "terminate:", "q"))
        self._status_item.setMenu_(menu)


_delegate = None  # NSApplication.delegate is weak; keep a strong reference


def main():
    global _delegate
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _delegate = AppDelegate.alloc().init()
    app.setDelegate_(_delegate)
    AppHelper.runEventLoop()
