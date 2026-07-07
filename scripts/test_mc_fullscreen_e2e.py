"""End-to-end test of closing a fullscreen window from Mission Control's
Spaces Bar. Sends TextEdit fullscreen (spaces will visibly animate), then
clicks our ✕ over its space tile.

    .venv/bin/python scripts/test_mc_fullscreen_e2e.py
"""

import os
import subprocess
import sys
import time

import ApplicationServices as AX
import Quartz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402

VICTIM_APP = "TextEdit"


def our_panels(app_pid):
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    panels = []
    for e in raw:
        if e.get(Quartz.kCGWindowOwnerPID) != app_pid:
            continue
        b = dict(e.get(Quartz.kCGWindowBounds) or {})
        if b.get("Width", 0) <= 60:
            panels.append({"x": b["X"], "y": b["Y"],
                           "w": b["Width"], "h": b["Height"]})
    return panels


def post_mouse(kind, x, y):
    event = Quartz.CGEventCreateMouseEvent(
        None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def press_escape():
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateKeyboardEvent(None, 53, down))


def main():
    project = os.path.join(os.path.dirname(__file__), "..")
    log_path = "/tmp/emc_fullscreen_app.log"
    log = open(log_path, "w")

    print("1. opening a TextEdit document and sending it fullscreen...")
    # Clear leftover state from any earlier (interrupted) run.
    subprocess.run(["osascript", "-e",
                    'tell application "TextEdit" to quit'], check=False)
    time.sleep(1.5)
    doc = "/tmp/emc_fullscreen_victim.txt"
    open(doc, "w").write("ExtraMissionControls fullscreen test victim\n")
    subprocess.run(["open", "-e", doc], check=False)
    time.sleep(2.0)

    fullscreen = False
    for attempt in range(14):
        if ax.find_fullscreen_window(VICTIM_APP) is not None:
            fullscreen = True
            break
        pids = ax._pids_named(VICTIM_APP)
        wins = ax._ax_windows(pids[0]) if pids else []
        if wins:
            AX.AXUIElementSetAttributeValue(wins[0], "AXFullScreen", True)
            time.sleep(2.5)  # fullscreen space animation
        else:
            time.sleep(0.8)
    if not fullscreen:
        raise SystemExit("FAIL: TextEdit did not enter fullscreen")
    print("   TextEdit is fullscreen")

    print("2. launching app...")
    app = subprocess.Popen(
        [os.path.join(project, ".venv/bin/python"), os.path.join(project, "main.py")],
        stdout=log, stderr=subprocess.STDOUT)
    time.sleep(2.5)

    ok = False
    try:
        print("3. triggering Mission Control...")
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        time.sleep(1.8)

        spaces = ax.mission_control_spaces()
        print("   spaces:", [(s["title"], int(s["x"]), int(s["y"])) for s in spaces])
        victim = next((s for s in spaces if s["title"] == VICTIM_APP), None)
        if victim is None:
            raise SystemExit("FAIL: no %r space tile" % VICTIM_APP)

        panels = our_panels(app.pid)
        print("   our ✕ panels on screen:", len(panels))
        target = min(panels, key=lambda p: (p["x"] - victim["x"]) ** 2
                                           + (p["y"] - victim["y"]) ** 2)
        dist = abs(target["x"] - victim["x"]) + abs(target["y"] - victim["y"])
        if dist > 60:
            raise SystemExit("FAIL: no ✕ panel near the %r space tile "
                             "(nearest is %.0f pts away)" % (VICTIM_APP, dist))

        cx, cy = target["x"] + target["w"] / 2, target["y"] + target["h"] / 2
        print("4. clicking ✕ at (%.0f, %.0f) over the space tile..." % (cx, cy))
        post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
        time.sleep(1.0)
        # Spaces bar may expand/re-layout on hover; re-read positions.
        spaces = ax.mission_control_spaces()
        victim = next((s for s in spaces if s["title"] == VICTIM_APP), victim)
        panels = our_panels(app.pid)
        target = min(panels, key=lambda p: (p["x"] - victim["x"]) ** 2
                                           + (p["y"] - victim["y"]) ** 2)
        cx, cy = target["x"] + target["w"] / 2, target["y"] + target["h"] / 2
        post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
        time.sleep(0.3)
        post_mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
        time.sleep(0.05)
        post_mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
        time.sleep(3.0)  # close + space removal animation

        # AX window lookups are unreliable while MC is open — dismiss first.
        press_escape()
        time.sleep(1.5)
        still_fullscreen = ax.find_fullscreen_window(VICTIM_APP) is not None
        pids = ax._pids_named(VICTIM_APP)
        remaining = len(ax._ax_windows(pids[0])) if pids else 0
        print("   after MC dismissed — still fullscreen: %s, windows left: %d"
              % (still_fullscreen, remaining))
        ok = not still_fullscreen and remaining == 0
    finally:
        print("5. cleaning up...")
        press_escape()
        time.sleep(0.6)
        app.terminate()
        subprocess.run(["osascript", "-e",
                        'tell application "TextEdit" to quit'], check=False)
        log.close()
        tail = open(log_path).read().strip()
        if tail:
            print("--- app output ---")
            print(tail)
        if "no closable" in tail:
            ok = False  # the app itself reported the close failed

    print("RESULT:", "PASS — ✕ closed the fullscreen window from the Spaces Bar"
          if ok else "FAIL — fullscreen window still open")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
