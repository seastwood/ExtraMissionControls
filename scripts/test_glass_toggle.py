#!/usr/bin/env python3
"""Unit test for the Liquid Glass toggle: OFF by default (energy efficiency),
every glass consumer falls back to its flat style when off, glass comes back
when enabled, and the controller's rebuild_style drops the pooled views so
the new style takes effect.

Headless: creates views/panels but orders nothing in; no Mission Control, no
screen. The user-defaults value is saved and restored around the test.

    python3 scripts/test_glass_toggle.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Foundation import NSMakeRect, NSUserDefaults

from extra_mission_controls import ui
from extra_mission_controls.mission_control import MissionControlButtons


def main():
    failures = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    defaults = NSUserDefaults.standardUserDefaults()
    original = defaults.objectForKey_(ui._GLASS_KEY)
    try:
        # Default: no stored preference -> glass OFF.
        defaults.removeObjectForKey_(ui._GLASS_KEY)
        ui._liquid_glass = None  # drop the cache; re-read from defaults
        check("default is OFF", not ui.liquid_glass_enabled())
        check("_make_glass yields None when off",
              ui._make_glass(NSMakeRect(0, 0, 24, 24), 12.0) is None)

        button = ui.make_close_button(None, None)
        check("button has no glass wrapper (draws its own disc)",
              button.outerView() is button)
        tray = ui.make_tray(NSMakeRect(0, 0, 100, 30), 9.0)
        check("tray falls back to a flat view",
              type(tray).__name__ != "NSGlassEffectView")
        root, view = ui.make_menu(NSMakeRect(0, 0, 158, 90))
        check("menu root is the flat MenuView itself", root is view)

        # Toggle ON: persisted, and glass comes back (macOS 26 has the class).
        ui.set_liquid_glass(True)
        check("setting persists to user defaults",
              bool(defaults.boolForKey_(ui._GLASS_KEY)))
        check("enabled after toggle", ui.liquid_glass_enabled())
        glass = ui._make_glass(NSMakeRect(0, 0, 24, 24), 12.0)
        check("_make_glass yields a glass view when on", glass is not None)
        button = ui.make_close_button(None, None)
        check("button gains its glass wrapper",
              button.outerView() is not button)

        # Toggle OFF again + rebuild drops the pooled views.
        ui.set_liquid_glass(False)
        c = MissionControlButtons.alloc().init()
        c._screen_height = 900.0
        c._panel_at(2)          # build a few pooled panels
        c._make_mark_panel()
        check("pool populated", len(c._panels) == 3)
        c.rebuild_style()
        check("rebuild_style empties the pools",
              c._panels == [] and c._buttons == [] and c._tray_panels == []
              and c._mark_panels == [] and c._menu_panel is None)
        c._panel_at(0)          # pools rebuild lazily afterwards
        check("pools rebuild on demand", len(c._panels) == 1)
    finally:
        if original is None:
            defaults.removeObjectForKey_(ui._GLASS_KEY)
        else:
            defaults.setObject_forKey_(original, ui._GLASS_KEY)
        ui._liquid_glass = None  # future readers see the restored value

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
