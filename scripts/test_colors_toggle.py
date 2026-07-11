#!/usr/bin/env python3
"""Unit test for the button Colors toggle: OFF by default (neutral dark disc),
ON gives the traffic-light hue — and it applies in BOTH the flat and the
Liquid Glass styles. Reads the disc layer's actual background colour to prove
the hue is present/absent.

Headless: builds buttons but orders nothing in; no Mission Control, no screen.
Saves/restores both the Colors and Liquid Glass user defaults.

    python3 scripts/test_colors_toggle.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Quartz
from Foundation import NSUserDefaults

from extra_mission_controls import ui


def bg(button):
    """(r, g, b, a) of the disc layer's background, or None. CGColorGetComponents
    returns an unbounded C array, so its length must come from
    CGColorGetNumberOfComponents — iterating it blindly walks off the end."""
    layer = button.layer()
    if layer is None:
        return None
    color = layer.backgroundColor()
    if color is None:
        return None
    n = Quartz.CGColorGetNumberOfComponents(color)
    comps = Quartz.CGColorGetComponents(color)
    if comps is None or n < 4:
        return None
    return tuple(comps[i] for i in range(n))


def styled_button(hover_rgb):
    b = ui.make_close_button(None, None)
    b.setHoverRGB_(hover_rgb)
    b.applyStyle()
    return b


def main():
    failures = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    defaults = NSUserDefaults.standardUserDefaults()
    orig_colors = defaults.objectForKey_(ui._COLORS_KEY)
    orig_glass = defaults.objectForKey_(ui._GLASS_KEY)
    try:
        # Default: no stored preference -> Colors ON.
        defaults.removeObjectForKey_(ui._COLORS_KEY)
        ui._button_colors = None
        check("default is ON (unset)", ui.colors_enabled())
        # An explicit False must still read as off (not confused with unset).
        ui.set_colors(False)
        ui._button_colors = None
        check("explicit off is respected", not ui.colors_enabled())

        # --- Flat style (Liquid Glass off) ---
        ui.set_liquid_glass(False)
        ui.set_colors(False)
        comps = bg(styled_button(ui.HOVER_RED))
        check("flat + colours off: neutral dark idle",
              comps is not None and max(comps[:3]) < 0.1)

        ui.set_colors(True)
        comps = bg(styled_button(ui.HOVER_RED))   # HOVER_RED r=1.0
        check("flat + colours on: red idle carries the hue",
              comps is not None and comps[0] > 0.8 and comps[2] < 0.5)
        comps_g = bg(styled_button(ui.HOVER_GREEN))  # green g dominant
        check("flat + colours on: green button is green",
              comps_g is not None and comps_g[1] > comps_g[0])

        # --- Liquid Glass style ---
        if getattr(__import__("AppKit"), "NSGlassEffectView", None) is not None:
            ui.set_liquid_glass(True)
            ui.set_colors(False)
            comps = bg(styled_button(ui.HOVER_RED))
            check("glass + colours off: neutral dark tint",
                  comps is not None and max(comps[:3]) < 0.1)
            ui.set_colors(True)
            comps = bg(styled_button(ui.HOVER_RED))
            check("glass + colours on: red tint carries the hue",
                  comps is not None and comps[0] > 0.8 and comps[2] < 0.5)
        else:
            print("  ..   (NSGlassEffectView unavailable — skipped glass cases)")

        # Persistence round-trips through user defaults.
        ui.set_colors(True)
        check("setting persists", bool(defaults.boolForKey_(ui._COLORS_KEY)))
        ui._button_colors = None
        check("re-reads persisted value", ui.colors_enabled())
    finally:
        for key, orig in ((ui._COLORS_KEY, orig_colors),
                          (ui._GLASS_KEY, orig_glass)):
            if orig is None:
                defaults.removeObjectForKey_(key)
            else:
                defaults.setObject_forKey_(orig, key)
        ui._button_colors = None
        ui._liquid_glass = None

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
