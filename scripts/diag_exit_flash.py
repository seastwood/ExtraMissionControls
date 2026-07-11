#!/usr/bin/env python3
"""Diagnose the exit-into-fullscreen button flash. Takes over the screen ~18s:

  1. opens a TextEdit victim window, sends it to full screen, switches to it
  2. launches EMC (EMC_DEBUG=1 so every gate decision is logged)
  3. opens Mission Control with the real ⌃↑ shortcut (no app activation, so
     the overview is genuinely the fullscreen space's — the user's scenario)
  4. exits MC with ⌃↑ again, sampling at ~60 Hz: EMC's on-screen panels AND
     every Dock window's (layer, alpha, height) — to find the FIRST
     WindowServer-side change of the exit animation (the AX tree is frozen)
  5. restores everything

    .venv/bin/python scripts/diag_exit_flash.py
"""
import os
import subprocess
import sys
import time

import Quartz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402

VICTIM_TITLE = "emc_flash_victim.txt"
GEOMETRY_LOG = "/tmp/extramissioncontrols-geometry.log"
KEY_UP_ARROW = 126


KEY_CONTROL = 59


def post_control_up():
    """The real Mission Control keyboard shortcut: ⌃↑ (toggles MC). Posted as
    a full sequence — Ctrl down, ↑ down, ↑ up, Ctrl up — because the symbolic
    hotkey ignores an arrow event that merely carries the Control flag."""
    ctrl_down = Quartz.CGEventCreateKeyboardEvent(None, KEY_CONTROL, True)
    Quartz.CGEventSetFlags(ctrl_down, Quartz.kCGEventFlagMaskControl)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ctrl_down)
    time.sleep(0.05)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, KEY_UP_ARROW, down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskControl)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.05)
    ctrl_up = Quartz.CGEventCreateKeyboardEvent(None, KEY_CONTROL, False)
    Quartz.CGEventSetFlags(ctrl_up, 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ctrl_up)
    time.sleep(0.05)


def frontmost_app():
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.localizedName() if app else "?"


def post_mouse(kind, x, y):
    event = Quartz.CGEventCreateMouseEvent(
        None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def sample_exit(app_pid, seconds=1.8):
    t0 = time.time()
    timeline = []
    while time.time() - t0 < seconds:
        timeline.append((time.time() - t0, panels_on_screen(app_pid)))
        time.sleep(1 / 60.0)
    flashes = [(t, n) for t, n in timeline if n > 0]
    print("   samples with panels visible: %d/%d"
          % (len(flashes), len(timeline)))
    if flashes:
        print("   visible window: %.3fs .. %.3fs, max %d panels"
              % (flashes[0][0], flashes[-1][0], max(n for _, n in flashes)))
    return timeline


def panels_on_screen(app_pid):
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    return sum(1 for e in raw
               if e.get(Quartz.kCGWindowOwnerPID) == app_pid
               and e.get(Quartz.kCGWindowLayer, 0) >= 100
               # ignore the invisible alpha-0 latch probe
               and float(e.get(Quartz.kCGWindowAlpha, 1.0)) > 0.05)


def all_windows():
    """(window number -> (owner, layer, alpha, w, h, x, y)) for every
    on-screen window — to find the FIRST WindowServer-side change when the
    Mission Control exit animation starts."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    out = {}
    for e in raw:
        bounds = dict(e.get(Quartz.kCGWindowBounds) or {})
        out[e.get(Quartz.kCGWindowNumber)] = (
            e.get(Quartz.kCGWindowOwnerName, "?"),
            e.get(Quartz.kCGWindowLayer, 0),
            round(float(e.get(Quartz.kCGWindowAlpha, 1.0)), 2),
            round(bounds.get("Width", 0)), round(bounds.get("Height", 0)),
            round(bounds.get("X", 0)), round(bounds.get("Y", 0)))
    return out


def dock_windows():
    """(window id -> (layer, alpha, height)) for on-screen Dock windows at
    Mission Control-ish layers. Alpha/height ramps reveal the exit animation."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    out = {}
    for e in raw:
        if e.get(Quartz.kCGWindowOwnerName) != "Dock":
            continue
        layer = e.get(Quartz.kCGWindowLayer, 0)
        if layer < 15:
            continue  # ignore the dock bar itself and low-level tiles
        bounds = dict(e.get(Quartz.kCGWindowBounds) or {})
        out[e.get(Quartz.kCGWindowNumber)] = (
            layer,
            round(float(e.get(Quartz.kCGWindowAlpha, 1.0)), 3),
            round(bounds.get("Height", 0)))
    return out


def victim_window():
    for pid in ax._pids_named("TextEdit"):
        for window in ax._ax_windows(pid):
            title = ax._attribute(window, "AXTitle") or ""
            if VICTIM_TITLE in title:
                return window
    return None


def main():
    project = os.path.join(os.path.dirname(__file__), "..")
    app_log = open("/tmp/emc_flash_app.log", "w")
    app = None
    fullscreened = False
    try:
        print("1. opening TextEdit victim...")
        subprocess.run(["osascript", "-e",
                        'tell application "TextEdit" to quit'], check=False)
        time.sleep(1.2)
        doc = "/tmp/" + VICTIM_TITLE
        open(doc, "w").write("EMC exit-flash diagnostic victim\n")
        subprocess.run(["open", "-e", doc], check=False)
        window = None
        for _ in range(20):
            time.sleep(0.5)
            window = victim_window()
            if window is not None:
                break
        if window is None:
            raise SystemExit("FAIL: victim window did not open")

        print("2. full-screening it and switching to its space...")
        if not ax.set_fullscreen(window, True):
            raise SystemExit("FAIL: could not set AXFullScreen")
        fullscreened = True
        time.sleep(2.0)
        for pid in ax._pids_named("TextEdit"):
            ax.activate_pid(pid)
        time.sleep(2.0)
        front = frontmost_app()
        print("   frontmost app now: %r (need TextEdit = we are ON the "
              "fullscreen space)" % front)
        if front != "TextEdit":
            raise SystemExit("FAIL: not on the fullscreen space")

        print("3. launching EMC with EMC_DEBUG=1...")
        env = dict(os.environ, EMC_DEBUG="1")
        app = subprocess.Popen(
            [os.path.join(project, ".venv/bin/python"),
             os.path.join(project, "main.py")],
            stdout=app_log, stderr=subprocess.STDOUT, env=env)
        time.sleep(2.0)
        if app.poll() is not None:
            raise SystemExit("FAIL: app exited early (see /tmp/emc_flash_app.log)")

        print("4. opening Mission Control from the fullscreen space...")
        t0 = time.time()
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        entry = []
        while time.time() - t0 < 3.0:
            entry.append((time.time() - t0, panels_on_screen(app.pid)))
            time.sleep(1 / 30.0)
        first_on = next((t for t, n in entry if n > 0), None)
        print("   ENTRY: buttons first visible at %s; last 5 samples: %s"
              % ("t=%.3fs" % first_on if first_on is not None else "never",
                 [n for _, n in entry[-5:]]))
        settled = [panels_on_screen(app.pid) for _ in range(5)]
        print("   panels while settled in MC:", settled)
        base_dock = dock_windows()
        print("   settled Dock windows (id: layer,alpha,h):",
              dict(sorted(base_dock.items())))

        print("5a. exit via TOGGLE (gesture-like: no click for the tap to see)")
        base_all = all_windows()
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        t0 = time.time()
        wide = []
        while time.time() - t0 < 1.2:
            wide.append((time.time() - t0, panels_on_screen(app.pid),
                         all_windows()))
            time.sleep(1 / 60.0)
        vis = [t for t, n, _ in wide if n > 0]
        print("   panels visible until t=%.3f" % (vis[-1] if vis else -1))
        # Earliest WindowServer-side change of any kind, and the first few.
        reported = 0
        for t, _, snap in wide:
            if snap == base_all:
                continue
            gone = set(base_all) - set(snap)
            new = set(snap) - set(base_all)
            changed = {k for k in set(base_all) & set(snap)
                       if base_all[k] != snap[k]}
            print("   t=%.3f: %d gone %d new %d changed" %
                  (t, len(gone), len(new), len(changed)))
            for k in list(changed)[:6]:
                print("      changed %s: %s -> %s" % (k, base_all[k], snap[k]))
            for k in list(gone)[:4]:
                print("      gone    %s: %s" % (k, base_all[k]))
            for k in list(new)[:4]:
                print("      new     %s: %s" % (k, snap[k]))
            base_all = snap
            reported += 1
            if reported >= 8:
                break
        print("   frontmost after exit: %r" % frontmost_app())

        time.sleep(1.5)
        print("5b. re-opening MC, then exit via CLICK on the fullscreen tile")
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        time.sleep(2.2)
        tile = next((s for s in ax.mission_control_spaces()
                     if s["title"] == "TextEdit"
                     or "emc_flash_victim" in (s["title"] or "")), None)
        if tile is None:
            print("   (no TextEdit tile found; skipping click phase)")
        else:
            cx = tile["x"] + tile["width"] / 2.0   # centre: clear of our buttons
            cy = tile["y"] + tile["height"] / 2.0
            print("   clicking tile %r centre (%.0f,%.0f)"
                  % (tile["title"], cx, cy))
            post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
            time.sleep(0.25)
            post_mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
            time.sleep(0.04)
            post_mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
            sample_exit(app.pid)
            print("   frontmost after exit: %r" % frontmost_app())
    finally:
        print("6. cleanup...")
        if app is not None:
            app.terminate()
        if fullscreened:
            time.sleep(0.8)
            fs = ax.find_fullscreen_window("TextEdit")
            if fs is not None:
                ax.set_fullscreen(fs, False)
                time.sleep(2.0)
        subprocess.run(["osascript", "-e",
                        'tell application "TextEdit" to quit'], check=False)
        app_log.close()

    print("\n--- GATE trace tail (%s) ---" % GEOMETRY_LOG)
    try:
        lines = open(GEOMETRY_LOG).read().splitlines()
        gate = [ln for ln in lines if ln.startswith(("GATE", "DEACTIVATE"))]
        for ln in gate[-45:]:
            print(ln)
    except OSError as exc:
        print("could not read log:", exc)


if __name__ == "__main__":
    main()
