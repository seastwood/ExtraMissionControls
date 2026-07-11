#!/usr/bin/env python3
"""Unit test for the near-fullscreen (zoom transient) suppression that stops the
✕/− buttons flashing on a window as it zooms in or out of a full-screen space.

Headless: builds no Mission Control, takes no screen, moves no mouse. It only
instantiates the controller, fakes the screen size, and exercises the pure
_zooming / _on_screen predicates and the on-screen filter they drive.

    python3 scripts/test_zoom_suppression.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extra_mission_controls.mission_control import MissionControlButtons

# A representative MacBook Air "looks like" logical resolution.
SCREEN_W, SCREEN_H = 1470.0, 956.0


def tile(x, y, w, h, title="Win"):
    return {"title": title, "x": x, "y": y, "width": w, "height": h}


def make_controller():
    c = MissionControlButtons.alloc().init()
    c._screen_width = SCREEN_W
    c._screen_height = SCREEN_H
    return c


def suppresses_all(c, thumbs):
    """Mirror the _collect_targets gate: a full-screen zoom transient anywhere
    suppresses every button (window AND Spaces Bar) until the transition ends."""
    return any(c._zooming(t) for t in thumbs)


def on_screen_filter(c, thumbs):
    """Mirror the (post-gate) on-screen filter: only center-on-screen thumbs."""
    return [t for t in thumbs if c._on_screen(t)]


def main():
    c = make_controller()
    failures = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    # A full-screen zoom transient: the window has grown to fill the display.
    fullscreen = tile(0, 0, SCREEN_W, SCREEN_H)
    check("fullscreen-sized thumb is a zoom transient",
          c._zooming(fullscreen))

    # The tail of the zoom, a hair under full size — still a transient.
    almost = tile(10, 10, SCREEN_W * 0.94, SCREEN_H * 0.93)
    check("94%/93% thumb is still a zoom transient", c._zooming(almost))

    # The ease-out can alias to "still" a touch before it completes; 0.85 in
    # both dims catches that ~88% frame (the exit-into-fullscreen flash).
    eased = tile(20, 20, SCREEN_W * 0.88, SCREEN_H * 0.88)
    check("88%/88% eased-out thumb is a zoom transient", c._zooming(eased))

    # A realistic single-window overview thumbnail (well under full size).
    single = tile(150, 200, SCREEN_W * 0.72, SCREEN_H * 0.70)
    check("72%/70% overview thumb is NOT a transient",
          not c._zooming(single))

    # A left-half real window shows in overview downscaled — never near-full.
    half = tile(120, 210, SCREEN_W * 0.36, SCREEN_H * 0.70)
    check("half-width overview thumb is NOT a transient",
          not c._zooming(half))

    # Wide but short (or tall but narrow) must NOT trip it — needs BOTH dims.
    wide_short = tile(0, 400, SCREEN_W, SCREEN_H * 0.5)
    check("full-width but short thumb is NOT a transient",
          not c._zooming(wide_short))

    # Guard is inert until the screen size is known (pre-activation).
    cold = MissionControlButtons.alloc().init()
    check("no screen size => never a transient",
          not cold._zooming(fullscreen))

    # The gate _collect_targets uses: a lone zooming thumbnail suppresses ALL
    # buttons (this is what kills the bar flash on exit into a full-screen app).
    check("lone zoom transient suppresses all buttons",
          suppresses_all(c, [fullscreen]))

    # Exiting into full screen from an overview that still has another window:
    # the growing window trips the gate even though a settled thumb is present.
    check("zoom transient among settled thumbs still suppresses all",
          suppresses_all(c, [single, fullscreen]))

    # A settled overview suppresses nothing.
    check("settled overview suppresses nothing",
          not suppresses_all(c, [single, half]))

    # The post-gate on-screen filter still drops panned-away thumbs.
    panned = tile(-SCREEN_W, 200, SCREEN_W * 0.72, SCREEN_H * 0.70)
    check("on-screen filter still drops panned-away thumbs",
          on_screen_filter(c, [single, panned]) == [single])

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
