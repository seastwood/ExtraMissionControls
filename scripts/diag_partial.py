#!/usr/bin/env python3
"""PASSIVE observer for Mission Control gestures, v2. Takes nothing over:
run it, then during its 30 seconds do — with the cursor mid-screen:
  1. three or four FAST three-finger flicks into MC (and back out)
  2. one slow gesture with a pause, then commit

At 30 Hz it records (only when something changes):
  - the AX tree (thumbnail frames + Spaces Bar tile) — full rate this time,
    to time the AX snap against the real windows' ease on fast flicks
  - the real windows' CG bounds (layer 0)
  - EMC panel visibility
It ALSO opens a LISTEN-ONLY event tap for scroll/gesture event types, to
learn whether trackpad gestures are visible to taps at all — if they are,
"fingers lifted" becomes a direct commit signal.

Output: stdout + /tmp/emc_partial_observe.log

    .venv/bin/python scripts/diag_partial.py
"""
import os
import sys
import threading
import time

import Quartz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402

LOG = "/tmp/emc_partial_observe.log"
DURATION = 30.0
T0 = time.time()
_out = open(LOG, "w")
_lock = threading.Lock()


def emit(line):
    with _lock:
        print(line, flush=True)
        _out.write(line + "\n")


# --- passive gesture/scroll event tap (listen-only, never consumes) --------
_seen_counts = {}


def _tap_cb(proxy, etype, event, refcon):
    now = time.time() - T0
    _seen_counts[etype] = _seen_counts.get(etype, 0) + 1
    if _seen_counts[etype] <= 40:  # don't flood on continuous streams
        phase = Quartz.CGEventGetIntegerValueField(event, 99)   # scroll phase
        momentum = Quartz.CGEventGetIntegerValueField(event, 123)
        emit("t=%6.2f  EVENT type=%d phase=%d momentum=%d" %
             (now, etype, phase, momentum))
    return event


def start_tap():
    # NSEventType gesture range + scroll: 22 scroll, 29 gesture, 30 magnify,
    # 31 swipe, 18 rotate, 19 beginGesture, 20 endGesture, 37/38 pressure-ish.
    mask = 0
    for t in (18, 19, 20, 22, 29, 30, 31, 37, 38):
        mask |= Quartz.CGEventMaskBit(t)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly, mask, _tap_cb, None)
    if tap is None:
        emit("EVENT TAP: could not create (permission?) — no gesture data")
        return
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    emit("EVENT TAP: listening for types 18,19,20,22,29,30,31,37,38")
    Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, DURATION, False)


def all_windows():
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    real, panels = {}, 0
    for e in raw:
        layer = e.get(Quartz.kCGWindowLayer, 0)
        owner = e.get(Quartz.kCGWindowOwnerName, "?")
        alpha = float(e.get(Quartz.kCGWindowAlpha, 1.0))
        if layer == 0:
            b = dict(e.get(Quartz.kCGWindowBounds) or {})
            real[e.get(Quartz.kCGWindowNumber)] = (
                round(b.get("X", 0)), round(b.get("Y", 0)),
                round(b.get("Width", 0)), round(b.get("Height", 0)))
        elif layer >= 100 and alpha > 0.05 and owner.lower().startswith("py"):
            panels += 1
    return real, panels


def watcher():
    prev_real, prev_panels, prev_ax = None, None, None
    while time.time() - T0 < DURATION:
        t = time.time() - T0
        real, panels = all_windows()
        group = ax.mission_control_group()
        ax_line = None
        if group is not None:
            thumbs, spaces, wc = ax.mission_control_snapshot(group)
            big = max(thumbs, key=lambda x: x["width"] * x["height"],
                      default=None)
            ax_line = (
                "big=%s bar=%s" % (
                    "%.0fx%.0f@(%.0f,%.0f)" % (big["width"], big["height"],
                                               big["x"], big["y"]) if big else "-",
                    "%.0f,%.0f %.0fx%.0f" % (spaces[0]["x"], spaces[0]["y"],
                                             spaces[0]["width"],
                                             spaces[0]["height"]) if spaces else "-"))
        state = (tuple(sorted(real.items())), panels, ax_line)
        if state != (prev_real, prev_panels, prev_ax):
            cg_bit = ""
            if prev_real is not None and tuple(sorted(real.items())) != prev_real:
                cg_bit = " CG-moving"
            emit("t=%6.2f panels=%-2d AX[%s]%s" % (t, panels, ax_line, cg_bit))
            prev_real = tuple(sorted(real.items()))
            prev_panels, prev_ax = panels, ax_line
        time.sleep(1 / 30.0)


def main():
    emit("observing %.0fs — fast flicks first, then one slow gesture..."
         % DURATION)
    w = threading.Thread(target=watcher, daemon=True)
    w.start()
    start_tap()          # runs the runloop for DURATION on the main thread
    w.join(timeout=2.0)
    emit("EVENT type counts: %s" % dict(sorted(_seen_counts.items())))
    _out.close()
    print("\nsaved to", LOG)


if __name__ == "__main__":
    main()
