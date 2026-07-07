"""End-to-end test of Mission Control ✕ buttons.

Launches the app, opens a sacrificial window, triggers Mission Control,
clicks our ✕ button over the victim's thumbnail with a synthetic mouse click,
and verifies the window closed. Briefly takes over the screen/mouse.

    .venv/bin/python scripts/test_mc_e2e.py             # Finder victim
    EMC_VICTIM=textedit .venv/bin/python scripts/test_mc_e2e.py

Finder keeps reporting AX windows while Mission Control is open (live-lookup
path); TextEdit hides them, like Notes/Steam do (held-reference path).
"""

import os
import subprocess
import sys
import time

import Quartz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402

if os.environ.get("EMC_VICTIM") == "textedit":
    VICTIM_OWNER = "TextEdit"
    VICTIM_TITLE = "emc_victim.txt"
else:
    VICTIM_OWNER = "Finder"
    VICTIM_TITLE = "Downloads"


def open_victim():
    if VICTIM_OWNER == "TextEdit":
        subprocess.run(["osascript", "-e",
                        'tell application "TextEdit" to quit'], check=False)
        time.sleep(1.5)
        doc = "/tmp/" + VICTIM_TITLE
        open(doc, "w").write("ExtraMissionControls test victim\n")
        subprocess.run(["open", "-e", doc], check=False)
    else:
        subprocess.run(["open", os.path.expanduser("~/Downloads")], check=False)


def cleanup_victim():
    if VICTIM_OWNER == "TextEdit":
        subprocess.run(["osascript", "-e",
                        'tell application "TextEdit" to quit'], check=False)


def victim_window_count():
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    return sum(1 for e in raw
               if e.get(Quartz.kCGWindowOwnerName) == VICTIM_OWNER
               and e.get(Quartz.kCGWindowLayer, 0) == 0
               and (e.get(Quartz.kCGWindowBounds) or {}).get("Width", 0) > 100)


def our_panels(app_pid):
    """CG windows owned by the app process, front-to-back order."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    panels = []
    for index, e in enumerate(raw):
        if e.get(Quartz.kCGWindowOwnerPID) != app_pid:
            continue
        b = dict(e.get(Quartz.kCGWindowBounds) or {})
        if b.get("Width", 0) <= 60:  # the little ✕ panels
            panels.append({"z": index, "x": b["X"], "y": b["Y"],
                           "w": b["Width"], "h": b["Height"]})
    return panels


def mc_backdrop_z():
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for index, e in enumerate(raw):
        if e.get(Quartz.kCGWindowOwnerName) == "Dock" \
                and e.get(Quartz.kCGWindowLayer, 0) in (18, 20):
            return index
    return None


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
    log_path = os.environ.get("EMC_TEST_LOG", "/tmp/emc_e2e_app.log")
    log = open(log_path, "w")

    print("1. opening sacrificial %s window..." % VICTIM_OWNER)
    open_victim()
    before = 0
    for _ in range(15):
        time.sleep(0.8)
        before = victim_window_count()
        if before:
            break
    print("   %s windows on screen: %d" % (VICTIM_OWNER, before))
    if before == 0:
        raise SystemExit("FAIL: victim window did not open")

    print("2. launching app...")
    app = subprocess.Popen(
        [os.path.join(project, ".venv/bin/python"), os.path.join(project, "main.py")],
        stdout=log, stderr=subprocess.STDOUT)
    time.sleep(2.5)
    if app.poll() is not None:
        raise SystemExit("FAIL: app exited early (see %s)" % log_path)

    ok = False
    try:
        print("3. triggering Mission Control...")
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        time.sleep(1.8)  # MC animation + a few app poll ticks

        thumbs = ax.mission_control_thumbnails()
        print("   thumbnails:", [(t["title"], int(t["x"]), int(t["y"])) for t in thumbs])
        panels = our_panels(app.pid)
        print("   our ✕ panels on screen:", len(panels))
        if not panels:
            raise SystemExit("FAIL: no ✕ panels appeared over Mission Control")

        backdrop_z = mc_backdrop_z()
        above = [p for p in panels if backdrop_z is None or p["z"] < backdrop_z]
        print("   panels above MC backdrop: %d/%d" % (len(above), len(panels)))

        victim = next((t for t in thumbs if t["title"] == VICTIM_TITLE), None)
        if victim is None:
            raise SystemExit("FAIL: no %r thumbnail found" % VICTIM_TITLE)

        # Hover first: MC zooms the thumbnail, our app re-tracks at 4 Hz.
        hx, hy = victim["x"] + 16, victim["y"] + 16
        print("4. hovering near ✕ at (%.0f, %.0f)..." % (hx, hy))
        post_mouse(Quartz.kCGEventMouseMoved, hx, hy)
        time.sleep(1.0)

        # Re-read our panel positions and click the one nearest the victim.
        thumbs = ax.mission_control_thumbnails()
        victim = next((t for t in thumbs if t["title"] == VICTIM_TITLE), victim)
        panels = our_panels(app.pid)
        target = min(panels, key=lambda p: (p["x"] - victim["x"]) ** 2
                                           + (p["y"] - victim["y"]) ** 2)
        cx, cy = target["x"] + target["w"] / 2, target["y"] + target["h"] / 2
        print("5. clicking ✕ panel at (%.0f, %.0f)..." % (cx, cy))
        post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
        time.sleep(0.3)
        post_mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
        time.sleep(0.05)
        post_mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
        time.sleep(1.2)

        after_in_mc = victim_window_count()
        mc_still_up = ax.mission_control_group() is not None
        print("   %s windows after click: %d | MC still active: %s"
              % (VICTIM_OWNER, after_in_mc, mc_still_up))
        ok = after_in_mc < before
    finally:
        print("6. dismissing Mission Control, stopping app...")
        press_escape()
        time.sleep(0.6)
        app.terminate()
        cleanup_victim()
        log.close()
        tail = open(log_path).read().strip()
        if tail:
            print("--- app output ---")
            print(tail)

    print("RESULT:", "PASS — ✕ button closed the window through Mission Control"
          if ok else "FAIL — window did not close")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
