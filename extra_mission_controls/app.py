"""Menu-bar app shell: status item, permission prompts, and the run loop."""

import os
import shlex
import subprocess
import sys

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBaselineOffsetAttributeName,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSFontWeightRegular,
    NSImage,
    NSImageLeft,
    NSImageOnly,
    NSImageSymbolConfiguration,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWorkspace,
)
from Foundation import NSBundle, NSObject, NSTimer, NSURL, NSUserDefaults
from PyObjCTools import AppHelper

from . import __version__, ax, ui, updates
from .mission_control import MissionControlButtons

UPDATE_CHECK_INTERVAL = 6 * 3600.0  # re-check GitHub every 6 hours

_ICON_KEY = "EMCTrayIcon"
# (SF Symbol name, menu label); the first entry is the default. All verified
# present on macOS 26 — unresolvable names are skipped when building the
# picker, and _apply_tray_icon falls back to the default.
TRAY_ICONS = (
    ("rectangle.grid.2x2", "Grid"),
    ("rectangle.3.group", "Overview"),
    ("rectangle.on.rectangle", "Overlapping"),
    ("rectangle.stack", "Stack"),
    ("macwindow", "Window"),
    ("macwindow.on.rectangle", "Desktop"),
    ("rectangle.badge.xmark", "Close Badge"),
)


def _current_icon_name():
    name = NSUserDefaults.standardUserDefaults().stringForKey_(_ICON_KEY)
    if name in tuple(symbol for symbol, _ in TRAY_ICONS):
        return name
    return TRAY_ICONS[0][0]


def _symbol_item(target, title, action, key, symbol_name):
    """Menu item with an SF Symbol in the ✓/state slot.

    On macOS 26 a regular item image gets its own indented icon column and
    pushes the title right of the other rows; the ON-state image is the one
    slot guaranteed to draw where the checkboxes' ✓ does. So the item is
    marked permanently ON with the symbol as its checkmark, sized down to
    checkmark metrics so the title isn't nudged either.
    """
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title, action, key)
    item.setTarget_(target)
    symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        symbol_name, title)
    if symbol is not None:
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            12.0, NSFontWeightRegular)
        sized = symbol.imageWithSymbolConfiguration_(config)
        item.setState_(NSControlStateValueOn)
        item.setOnStateImage_(sized if sized is not None else symbol)
    return item


class AppDelegate(NSObject):

    def applicationDidFinishLaunching_(self, notification):
        self.mission_control = MissionControlButtons.alloc().init()
        self.mission_control.start()
        self._setup_status_item()

        # Release notifier: first check shortly after launch (keep startup
        # snappy), then periodically while the app stays up.
        self._update_item = None
        self._update_separator = None
        self._update_url = None
        self._update_version = None
        self.performSelector_withObject_afterDelay_("checkForUpdates:", None,
                                                    10.0)
        self._update_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                UPDATE_CHECK_INTERVAL, self, "checkForUpdates:", None, True))

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
        self._apply_tray_icon()

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
        # Tray-icon picker: each entry previews its symbol (the per-item icon
        # column is fine here — every row has one, so the labels align).
        icon_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Icon", None, "")
        icon_menu = NSMenu.alloc().init()
        current = _current_icon_name()
        for symbol_name, label in TRAY_ICONS:
            preview = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol_name, label)
            if preview is None:
                continue
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "selectTrayIcon:", "")
            entry.setTarget_(self)
            entry.setRepresentedObject_(symbol_name)
            entry.setImage_(preview)
            entry.setState_(NSControlStateValueOn if symbol_name == current
                            else NSControlStateValueOff)
            icon_menu.addItem_(entry)
        icon_item.setSubmenu_(icon_menu)
        menu.addItem_(icon_item)
        self._icon_menu = icon_menu
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(_symbol_item(self, "Restart", "restartApp:", "",
                                   "arrow.clockwise"))
        # Our own selector, not "terminate:": macOS 26 decorates the stock
        # terminate item with a system icon in the indented icon column,
        # which pushes the title out of line with the rows above. No ⌘Q key
        # equivalent — its column alone added ~47pt of menu width.
        menu.addItem_(_symbol_item(self, "Quit", "quitApp:", "", "power"))
        self._status_item.setMenu_(menu)

    def quitApp_(self, sender):
        NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _apply_tray_icon(self):
        button = self._status_item.button()
        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            _current_icon_name(), "ExtraMissionControls")
        if symbol is None:
            symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                TRAY_ICONS[0][0], "ExtraMissionControls")
        if symbol is not None:
            button.setImage_(symbol)
        else:
            button.setTitle_("⧉")

    def selectTrayIcon_(self, sender):
        name = sender.representedObject()
        NSUserDefaults.standardUserDefaults().setObject_forKey_(name, _ICON_KEY)
        self._apply_tray_icon()
        for index in range(self._icon_menu.numberOfItems()):
            entry = self._icon_menu.itemAtIndex_(index)
            entry.setState_(NSControlStateValueOn
                            if entry.representedObject() == name
                            else NSControlStateValueOff)

    def checkForUpdates_(self, sender):
        updates.check_async(self._show_update_available)

    def _show_update_available(self, version, url):
        if version == updates.ignored_version():
            return
        self._update_url = url
        self._update_version = version
        title = "Update Available — %s" % version
        if self._update_item is not None:
            self._update_item.setTitle_(title)
            return
        # Red dot beside the tray icon. An attributed title keeps its color
        # (a plain title would be tinted like a template image).
        button = self._status_item.button()
        dot = NSAttributedString.alloc().initWithString_attributes_(
            " ●", {
                NSForegroundColorAttributeName: NSColor.systemRedColor(),
                NSFontAttributeName: NSFont.systemFontOfSize_(8.0),
                NSBaselineOffsetAttributeName: 5.0,
            })
        button.setImagePosition_(NSImageLeft)
        button.setAttributedTitle_(dot)

        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, None, "")
        submenu = NSMenu.alloc().init()
        view = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "View Release Page", "openReleasePage:", "")
        view.setTarget_(self)
        submenu.addItem_(view)
        ignore = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Ignore This Version", "ignoreUpdate:", "")
        ignore.setTarget_(self)
        submenu.addItem_(ignore)
        item.setSubmenu_(submenu)

        separator = NSMenuItem.separatorItem()
        menu = self._status_item.menu()
        menu.insertItem_atIndex_(item, 0)
        menu.insertItem_atIndex_(separator, 1)
        self._update_item = item
        self._update_separator = separator

    def openReleasePage_(self, sender):
        if self._update_url:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_(self._update_url))

    def ignoreUpdate_(self, sender):
        """Skip this release: drop the badge and menu entry, and remember the
        version so only a release newer than it re-notifies."""
        if self._update_version:
            updates.set_ignored_version(self._update_version)
        menu = self._status_item.menu()
        if self._update_item is not None:
            menu.removeItem_(self._update_item)
            self._update_item = None
        if self._update_separator is not None:
            menu.removeItem_(self._update_separator)
            self._update_separator = None
        button = self._status_item.button()
        button.setTitle_("")
        button.setImagePosition_(NSImageOnly)

    def restartApp_(self, sender):
        """Relaunch the app: a detached shell waits for this process to exit,
        then starts a fresh copy (waiting avoids two instances racing for the
        status item and the AX observers)."""
        if "RESOURCEPATH" in os.environ:  # set by the py2app bootstrap
            bundle = NSBundle.mainBundle().bundlePath()
            relaunch = "/usr/bin/open -n %s" % shlex.quote(bundle)
        else:  # running from source: python main.py
            argv = [sys.executable, os.path.abspath(sys.argv[0])]
            argv += sys.argv[1:]
            relaunch = " ".join(shlex.quote(arg) for arg in argv)
        subprocess.Popen(
            ["/bin/sh", "-c",
             "while /bin/kill -0 %d 2>/dev/null; do sleep 0.2; done; exec %s"
             % (os.getpid(), relaunch)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        NSApplication.sharedApplication().terminate_(None)

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
