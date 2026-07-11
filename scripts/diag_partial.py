#!/usr/bin/env python3
"""PASSIVE observer for partial Mission Control gestures. Takes nothing over:
run it, then do slow three-finger gestures yourself for ~30 seconds —
pause part-way in, hold a few seconds, continue in; pause part-way out, hold,
continue. Do it from the DESKTOP (that's the blind spot).

At 30 Hz it diffs the ENTIRE on-screen window list (every owner, layer,
alpha, bounds) and logs each change, plus what the Dock's AX tree reports at
the same moment (thumbnail count/first frame) at ~5 Hz. This finds the signal
that distinguishes 'gesture paused mid-flight' from 'fully settled'.

Output: stdout + /tmp/emc_partial_observe.log

    .venv/bin/python scripts/diag_partial.py
"""
import os
import sys
import time

import Quartz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402

LOG = "/tmp/emc_partial_observe.log"
DURATION = 30.0


def all_windows():
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    out = {}
    for e in raw:
        b = dict(e.get(Quartz.kCGWindowBounds) or {})
        out[e.get(Quartz.kCGWindowNumber)] = (
            e.get(Quartz.kCGWindowOwnerName, "?"),
            e.get(Quartz.kCGWindowLayer, 0),
            round(float(e.get(Quartz.kCGWindowAlpha, 1.0)), 2),
            round(b.get("X", 0)), round(b.get("Y", 0)),
            round(b.get("Width", 0)), round(b.get("Height", 0)))
    return out


def fmt(entry):
    owner, layer, alpha, x, y, w, h = entry
    return "%s L%d a%.2f %dx%d@(%d,%d)" % (owner, layer, alpha, w, h, x, y)


def main():
    out = open(LOG, "w")

    def emit(line):
        print(line, flush=True)
        out.write(line + "\n")

    emit("observing %.0fs — slow gestures from the DESKTOP now, with pauses..."
         % DURATION)
    t0 = time.time()
    prev = all_windows()
    last_ax = 0.0
    while time.time() - t0 < DURATION:
        t = time.time() - t0
        cur = all_windows()
        gone = set(prev) - set(cur)
        new = set(cur) - set(prev)
        changed = {k for k in set(prev) & set(cur) if prev[k] != cur[k]}
        if gone or new or changed:
            present, settled = ax.mission_control_backdrop_state()
            emit("t=%6.2f  (present=%d settled=%d)" % (t, present, settled))
            for k in sorted(new):
                emit("   new     #%d %s" % (k, fmt(cur[k])))
            for k in sorted(gone):
                emit("   gone    #%d %s" % (k, fmt(prev[k])))
            for k in sorted(changed):
                emit("   changed #%d %s -> %s" % (k, fmt(prev[k]), fmt(cur[k])))
        prev = cur
        if time.time() - last_ax > 0.2:
            last_ax = time.time()
            group = ax.mission_control_group()
            if group is not None:
                thumbs, spaces, wc = ax.mission_control_snapshot(group)
                big = max(thumbs, key=lambda x: x["width"] * x["height"],
                          default=None)
                emit("t=%6.2f  AX: thumbs=%d wc=%d big=%s spaces0=%s"
                     % (t, len(thumbs), wc,
                        "%.0fx%.0f@(%.0f,%.0f)" % (big["width"], big["height"],
                                                   big["x"], big["y"])
                        if big else "-",
                        "%.0f,%.0f %.0fx%.0f" % (spaces[0]["x"], spaces[0]["y"],
                                                 spaces[0]["width"],
                                                 spaces[0]["height"])
                        if spaces else "-"))
        time.sleep(1 / 30.0)
    out.close()
    print("\nsaved to", LOG)


if __name__ == "__main__":
    main()
