"""Dev probe: create a Split View (two windows sharing one fullscreen space),
open Mission Control, and dump what the Dock's AX tree exposes for it — so we
can support closing a specific app in a Split View tile.

Takes over screen/mouse for ~20s, then cleans up (quits TextEdit).

    .venv/bin/python scripts/probe_splitview.py
"""

import subprocess
import sys
import time

import ApplicationServices as AX
import Quartz

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from extra_mission_controls import ax  # noqa: E402


def key(code, flags=0):
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.05)


def mouse(kind, x, y):
    e = Quartz.CGEventCreateMouseEvent(
        None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def drag(x1, y1, x2, y2):
    mouse(Quartz.kCGEventMouseMoved, x1, y1)
    time.sleep(0.3)
    mouse(Quartz.kCGEventLeftMouseDown, x1, y1)
    time.sleep(0.6)  # let MC pick up the thumbnail
    steps = 24
    for i in range(1, steps + 1):
        mouse(Quartz.kCGEventLeftMouseDragged,
              x1 + (x2 - x1) * i / steps,
              y1 + (y2 - y1) * i / steps)
        time.sleep(0.04)
    time.sleep(0.5)
    mouse(Quartz.kCGEventLeftMouseUp, x2, y2)
    time.sleep(0.8)


def describe(el, depth, maxdepth=4):
    role = ax._attribute(el, AX.kAXRoleAttribute)
    sub = ax._attribute(el, AX.kAXSubroleAttribute)
    title = ax._attribute(el, AX.kAXTitleAttribute)
    desc = ax._attribute(el, AX.kAXDescriptionAttribute)
    ident = ax._attribute(el, "AXIdentifier")
    pos = ax._unpack_point(ax._attribute(el, AX.kAXPositionAttribute))
    size = ax._unpack_size(ax._attribute(el, AX.kAXSizeAttribute))
    err, actions = AX.AXUIElementCopyActionNames(el, None)
    bits = ["%s%s" % (role, "/" + sub if sub else "")]
    if ident:
        bits.append("id=%s" % ident)
    if title:
        bits.append("title=%r" % title)
    if desc:
        bits.append("desc=%r" % desc)
    if pos and size:
        bits.append("@(%.0f,%.0f %.0fx%.0f)" % (pos[0], pos[1], size[0], size[1]))
    if actions:
        bits.append("actions=%s" % list(actions))
    print("  " * depth + "  ".join(bits))
    if depth < maxdepth:
        for child in ax._attribute(el, AX.kAXChildrenAttribute) or []:
            describe(child, depth + 1, maxdepth)


def dump_mc(label):
    print("\n===== %s =====" % label)
    group = ax.mission_control_group()
    if group is None:
        print("  (Mission Control not active)")
        return
    print("-- Spaces Bar tiles:")
    for s in ax.mission_control_spaces(group):
        describe(s["element"], 1)
    print("-- Window thumbnails (mc.windows):")
    for t in ax.mission_control_thumbnails(group):
        print("    title=%r @(%.0f,%.0f %.0fx%.0f)"
              % (t["title"], t["x"], t["y"], t["width"], t["height"]))


def main():
    if not ax.is_trusted():
        raise SystemExit("Needs Accessibility permission.")

    print("1. opening two TextEdit windows...")
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit'],
                   check=False)
    time.sleep(1.5)
    for name in ("emc_split_a.txt", "emc_split_b.txt"):
        path = "/tmp/" + name
        open(path, "w").write("Split View probe: %s\n" % name)
        subprocess.run(["open", "-e", path], check=False)
        time.sleep(1.5)

    pids = ax._pids_named("TextEdit")
    wins = ax._ax_windows(pids[0]) if pids else []
    print("   TextEdit windows:", len(wins))
    if len(wins) < 2:
        raise SystemExit("FAIL: need two TextEdit windows")

    print("2. fullscreening the first window...")
    ax.set_fullscreen(wins[0], True)
    time.sleep(2.5)

    print("3. switching back to Desktop...")
    key(123, Quartz.kCGEventFlagMaskControl)  # Ctrl+Left
    time.sleep(1.5)

    print("4. opening Mission Control...")
    subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
    time.sleep(1.8)
    dump_mc("BEFORE split (desktop current)")

    group = ax.mission_control_group()
    spaces = ax.mission_control_spaces(group) if group else []
    thumbs = ax.mission_control_thumbnails(group) if group else []
    fs_tile = next((s for s in spaces if s["title"] == "TextEdit"), None)
    thumb = thumbs[0] if thumbs else None
    if fs_tile and thumb:
        print("5. dragging window thumbnail onto the TextEdit fullscreen tile...")
        drag(thumb["x"] + thumb["width"] / 2, thumb["y"] + thumb["height"] / 2,
             fs_tile["x"] + fs_tile["width"] / 2, fs_tile["y"] + fs_tile["height"] / 2)
        time.sleep(1.5)
        dump_mc("AFTER split-view drag")
    else:
        print("   could not find tile/thumbnail to drag (fs_tile=%s thumb=%s)"
              % (bool(fs_tile), bool(thumb)))

    print("\n6. cleanup...")
    key(53)  # Escape
    time.sleep(0.6)
    key(53)
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to quit'],
                   check=False)
    print("done")


if __name__ == "__main__":
    main()
