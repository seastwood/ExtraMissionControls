#!/usr/bin/env python3
"""Read-only probe: dump each running app's Window menu (and any "Move & Resize"
submenu) so we can confirm the native-tiling menu items exist and their exact
titles on this macOS. It ONLY reads the Accessibility menu tree — it presses
nothing, moves no windows, and takes no screen.

Run it from an environment that already has Accessibility permission (e.g. the
PyCharm run config used for main.py); a process without permission sees empty
menus.

    python3 scripts/probe_tile_menu.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ApplicationServices as AX
from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace

from extra_mission_controls import ax


def dump_menu(app_name, pid):
    element = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(element, 0.5)
    menu_bar = ax._attribute(element, "AXMenuBar")
    if menu_bar is None:
        return  # no AX menu bar (agent app, or permission missing)
    window_menu = ax._menu_item_titled(
        ax._attribute(menu_bar, AX.kAXChildrenAttribute) or [], ("Window",))
    if window_menu is None:
        print("  %s: no Window menu" % app_name)
        return
    items = ax._submenu_items(window_menu)
    titles = [ax._attribute(i, AX.kAXTitleAttribute) or "" for i in items]
    print("  %s: Window ▸ %s" % (app_name, [t for t in titles if t]))
    submenu = ax._menu_item_titled(items, ("Move & Resize",))
    if submenu is not None:
        sub = ax._submenu_items(submenu)
        for i in sub:
            t = ax._attribute(i, AX.kAXTitleAttribute) or ""
            if not t:
                continue
            enabled = ax._attribute(i, "AXEnabled")
            print("        Move & Resize ▸ %-24s enabled=%s" % (t, enabled))


def main():
    if not ax.is_trusted():
        print("NOT Accessibility-trusted — run from a permitted environment "
              "(menus will read empty otherwise).")
    ws = NSWorkspace.sharedWorkspace()
    apps = [a for a in ws.runningApplications()
            if a.activationPolicy() == NSApplicationActivationPolicyRegular]
    print("Standard running apps: %d\n" % len(apps))
    for a in sorted(apps, key=lambda a: a.localizedName() or ""):
        dump_menu(a.localizedName() or "?", a.processIdentifier())
    return 0


if __name__ == "__main__":
    sys.exit(main())
