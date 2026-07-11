#!/usr/bin/env python3
"""Unit test for the probable-exit latch in _collect_targets: it must hide
everything while the layout stays frozen at the latch-time signature, and
release on EITHER movement (Mission Control survived) OR expiry
(_EXIT_LATCH_MAX — a false positive, typically the entry zoom, must never
hide the buttons indefinitely; the user hit exactly that).

Headless: ax.mission_control_* readers are patched to return synthetic
layouts; no Mission Control, no screen, no mouse.

    python3 scripts/test_exit_latch.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extra_mission_controls import ax, mission_control
from extra_mission_controls.mission_control import MissionControlButtons

SCREEN_W, SCREEN_H = 1470.0, 956.0


def thumb(x=100):
    return {"title": "Songs", "x": x, "y": 300, "width": 400,
            "height": 260, "element": None}


def bar_tile(settled=True):
    """A Spaces Bar tile: full-size at y=46 when settled (the commit state),
    or the scaled-down off-screen strip seen throughout a drag gesture."""
    if settled:
        return {"title": "Desktop", "desc": "", "x": 411, "y": 46,
                "width": 138, "height": 90, "element": None}
    return {"title": "Desktop", "desc": "", "x": 582, "y": -32,
            "width": 65, "height": 24, "element": None}


def make_controller():
    c = MissionControlButtons.alloc().init()
    c._screen_width = SCREEN_W
    c._screen_height = SCREEN_H
    return c


def frame(c, thumbs, spaces=None, cg=None):
    """One _collect_targets pass against a synthetic layout. The bar defaults
    to settled, whose False->True transition fast-accepts the thumbs layout.
    `cg` mimics the real-window bounds signature the sync tick's pulse fills
    in (used by the commit-ease detector)."""
    if spaces is None:
        spaces = [bar_tile(settled=True)]
    if cg is not None:
        c._cg_sig = cg
    ax.mission_control_snapshot = lambda group: (thumbs, spaces, 1)
    return c._collect_targets(object())


def confirm_settle(c):
    """Backdate the settle candidate past _SETTLE_CONFIRM, as if the layout
    had already held still that long."""
    c._candidate_since -= (mission_control._SETTLE_CONFIRM + 0.05)


def main():
    failures = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    saved = ax.mission_control_snapshot
    try:
        c = make_controller()
        targets, marks = frame(c, [thumb()])
        check("first frame is the baseline (no buttons)", targets == [])
        targets, _ = frame(c, [thumb()])
        check("settled layout emits window buttons", len(targets) == 5)

        # A latch while the layout stays frozen: hidden.
        c._latch_exit()
        targets, marks = frame(c, [thumb()])
        check("latched + frozen layout -> nothing", (targets, marks) == ([], []))
        targets, marks = frame(c, [thumb()])
        check("still latched next frame", (targets, marks) == ([], []))

        # Movement releases it (Mission Control survived, e.g. a drag)...
        frame(c, [thumb(x=140)])   # release frame (layout moved; also baseline)
        check("movement releases the latch", not c._exit_latched)
        # The moved layout is NEW, so it must first earn trust: one still
        # frame starts the settle candidate, and only after _SETTLE_CONFIRM
        # (backdated here) do the buttons return.
        targets, _ = frame(c, [thumb(x=140)])
        check("new layout needs settle confirmation first", targets == [])
        confirm_settle(c)
        targets, _ = frame(c, [thumb(x=140)])
        check("buttons come back after the layout is confirmed",
              len(targets) == 5)

        # ...and so does expiry, even with the layout still frozen — the
        # false-positive case (entry zoom) that hid the buttons indefinitely.
        c._latch_exit()
        targets, _ = frame(c, [thumb(x=140)])
        check("latched again (frozen)", targets == [])
        c._exit_latch_expires = time.time() - 0.01   # simulate _EXIT_LATCH_MAX
        targets, _ = frame(c, [thumb(x=140)])
        check("expiry releases the latch with a frozen layout",
              len(targets) == 5 and not c._exit_latched)

        # Scene gate: while the invisible probe reads the scene as
        # mid-transition (streak below the minimum), nothing is emitted at
        # all — buttons only exist when they are actually clickable.
        check("gate initialises open (headless)",
              c._scene_streak >= mission_control._SCENE_STREAK_MIN)
        c._scene_streak = 0
        targets, marks = frame(c, [thumb(x=140)])
        check("closed scene gate emits nothing", (targets, marks) == ([], []))
        c._scene_streak = mission_control._SCENE_STREAK_MIN
        targets, _ = frame(c, [thumb(x=140)])
        check("reopened scene gate emits again", len(targets) == 5)

        # A latch taken before any frame exists adopts the first layout the
        # latch block sees (never a match-anything permanent hide). The very
        # first frame is swallowed by the baseline gate, so adoption happens
        # on the second.
        c2 = make_controller()
        c2._latch_exit()             # _prev_layout is None here
        targets, _ = frame(c2, [thumb()])   # baseline frame: hidden anyway
        check("pre-baseline latch frame stays hidden", targets == [])
        targets, _ = frame(c2, [thumb()])   # latch block runs: adopts the sig
        check("adopted signature keeps it latched while frozen",
              targets == [] and c2._exit_latch_sig is not None)
        frame(c2, [thumb(x=200)])
        check("adopted signature still releases on movement",
              not c2._exit_latched)

        # THE desktop-gesture bug: a paused three-finger drag reads perfectly
        # still (macOS animates the real windows and AX tracks them live), but
        # the Spaces Bar stays scaled-down/off-screen until the gesture
        # commits — so no buttons may appear during the hold, however long
        # it reads "still", until the bar snaps settled.
        c3 = make_controller()
        frame(c3, [thumb()], [bar_tile(settled=False)])           # baseline
        targets, marks = [], []
        for _ in range(6):
            targets, marks = frame(c3, [thumb()], [bar_tile(settled=False)])
        check("paused mid-gesture (bar unsettled) never renders",
              (targets, marks) == ([], []))
        targets, _ = frame(c3, [thumb()], [bar_tile(settled=True)])
        check("bar snapping settled = the commit -> buttons render",
              len(targets) == 5)

        # Gesture-open where the thumbnails JUMP at commit (the real macOS
        # behaviour): bar snaps settled AND thumbs move to their final spots in
        # the SAME frame, so thumbs_still is False there. The fast-accept
        # window must carry to the first still frame after commit, so buttons
        # appear ~1 frame later — NOT after the 0.4s confirm.
        c5 = make_controller()
        frame(c5, [thumb(x=100)], [bar_tile(settled=False)])   # baseline
        frame(c5, [thumb(x=140)], [bar_tile(settled=False)])   # still gesturing
        targets, _ = frame(c5, [thumb(x=300)], [bar_tile(settled=True)])
        check("commit frame (thumbs jumped) holds off one frame", targets == [])
        targets, _ = frame(c5, [thumb(x=300)], [bar_tile(settled=True)])
        check("first still frame after a jump-commit renders (no 0.4s wait)",
              len(targets) == 5)

        # Cursor-independent commit: with the bar resting COLLAPSED (cursor
        # away from the top), the commit is identified by the AX layout being
        # still while the REAL windows' CG bounds keep easing — that pairing
        # exists only at commit. Buttons must render right then, not after
        # the 1.5s no-bar confirm.
        c6 = make_controller()
        frame(c6, [thumb(x=100)], [bar_tile(settled=False)], cg=("a",))
        frame(c6, [thumb(x=140)], [bar_tile(settled=False)], cg=("b",))
        targets, _ = frame(c6, [thumb(x=300)],
                           [bar_tile(settled=False)], cg=("c",))
        check("commit snap frame (thumbs moved) still hidden", targets == [])
        targets, _ = frame(c6, [thumb(x=300)],
                           [bar_tile(settled=False)], cg=("d",))
        check("AX-still + windows-easing = commit -> renders with bar collapsed",
              len(targets) == 5)

        # ...whereas a genuinely held gesture (AX and windows BOTH frozen)
        # opens nothing.
        c7 = make_controller()
        frame(c7, [thumb(x=100)], [bar_tile(settled=False)], cg=("a",))
        frame(c7, [thumb(x=100)], [bar_tile(settled=False)], cg=("a",))
        targets, _ = frame(c7, [thumb(x=100)],
                           [bar_tile(settled=False)], cg=("a",))
        check("frozen hold (AX and windows still) stays hidden", targets == [])

        # Fallback for a machine whose bar rests collapsed: a long frozen
        # hold (>= _SETTLE_CONFIRM_NOBAR) still opens the gate eventually.
        c4 = make_controller()
        frame(c4, [thumb()], [bar_tile(settled=False)])
        frame(c4, [thumb()], [bar_tile(settled=False)])
        c4._candidate_since -= (mission_control._SETTLE_CONFIRM_NOBAR + 0.05)
        targets, _ = frame(c4, [thumb()], [bar_tile(settled=False)])
        check("long frozen hold accepts even without the bar (fallback)",
              len(targets) == 5)
    finally:
        ax.mission_control_snapshot = saved

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
