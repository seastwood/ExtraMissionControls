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
from Foundation import NSBundle, NSObject, NSTimer, NSURL
from PyObjCTools import AppHelper

from . import __version__, ax, login, ui, updates
from .mission_control import MissionControlButtons

UPDATE_CHECK_INTERVAL = 6 * 3600.0  # re-check GitHub every 6 hours

def _tray_image(label="Extra Mission Controls"):
    """EMC's branded menu-bar icon (assets/tray_icon.png) as a template image,
    so macOS tints it for light and dark menu bars. None if it can't load."""
    base = os.environ.get("RESOURCEPATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
    image = NSImage.alloc().initWithContentsOfFile_(
        os.path.join(base, "tray_icon.png"))
    if image is not None:
        image.setSize_((18, 18))
        image.setTemplate_(True)
        image.setAccessibilityDescription_(label)
    return image


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

        if not ax.ensure_permissions():
            print("Extra Mission Controls: Accessibility permission missing — "
                  "nothing will work until it is granted "
                  "(System Settings → Privacy & Security → Accessibility).")

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
        # Liquid Glass is on by default (unset = on); unchecking it switches
        # to the lighter flat style.
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
        # Start EMC automatically when you log in (a per-user LaunchAgent).
        login_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open at Login", "toggleOpenAtLogin:", "")
        login_item.setTarget_(self)
        login_item.setState_(NSControlStateValueOn if login.enabled()
                             else NSControlStateValueOff)
        menu.addItem_(login_item)
        self._login_item = login_item
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
        image = _tray_image()
        if image is not None:
            button.setImage_(image)
        else:
            button.setTitle_("EMC")

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

    def toggleOpenAtLogin_(self, sender):
        state = login.set_enabled(not login.enabled())
        self._login_item.setState_(NSControlStateValueOn if state
                                   else NSControlStateValueOff)

    def toggleColors_(self, sender):
        ui.set_colors(not ui.colors_enabled())
        self._colors_item.setState_(
            NSControlStateValueOn if ui.colors_enabled()
            else NSControlStateValueOff)
        self.mission_control.rebuild_style()


_delegate = None  # NSApplication.delegate is weak; keep a strong reference


_PID_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"),
                         "extramissioncontrols.pid")


def _replace_running_instance():
    """Only one EMC may run: two draw duplicate buttons and tray icons. A
    copy left behind (a suspended Ctrl+Z, a Terminal that kept it alive) is
    stopped here so a fresh launch always wins with the current code."""
    import signal
    import time
    try:
        with open(_PID_FILE) as fh:
            old = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid():
        try:
            # Only stop it if it really is EMC (the pid may have been reused).
            cmd = subprocess.run(["ps", "-p", str(old), "-o", "command="],
                                 capture_output=True, text=True).stdout
        except OSError:
            cmd = ""
        if ("main.py" in cmd or "ExtraMissionControls" in cmd
                or "Extra Mission Controls" in cmd):
            for sig in (signal.SIGCONT, signal.SIGTERM):
                try:
                    os.kill(old, sig)
                except OSError:
                    break
            for _ in range(20):  # up to 1s for a clean exit, then force it
                try:
                    os.kill(old, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(old, signal.SIGKILL)
                except OSError:
                    pass
    try:
        with open(_PID_FILE, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def main():
    global _delegate
    _replace_running_instance()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _delegate = AppDelegate.alloc().init()
    app.setDelegate_(_delegate)
    # Ctrl+C / kill quit immediately. The Cocoa run loop never hands SIGINT
    # to Python's default handler (KeyboardInterrupt), and PyObjC's
    # installInterrupt didn't fire on macOS 27; a plain handler runs on the
    # next Python callback (EMC's poll timers tick several times a second).
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: os._exit(0))
    AppHelper.runEventLoop()
