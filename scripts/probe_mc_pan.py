"""Dev probe: what does the Dock's AX tree report after panning INSIDE
Mission Control to a fullscreen space? (✕ buttons misplace there.)

Takes over the screen ~15s: opens MC, pans right, dumps, pans back, escapes.

    .venv/bin/python scripts/probe_mc_pan.py
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


def walk(el, depth=0, maxdepth=5):
    role = ax._attribute(el, AX.kAXRoleAttribute)
    ident = ax._attribute(el, "AXIdentifier")
    title = ax._attribute(el, AX.kAXTitleAttribute)
    desc = ax._attribute(el, AX.kAXDescriptionAttribute)
    pos = ax._unpack_point(ax._attribute(el, AX.kAXPositionAttribute))
    size = ax._unpack_size(ax._attribute(el, AX.kAXSizeAttribute))
    bits = [str(role)]
    if ident:
        bits.append("id=%s" % ident)
    if title:
        bits.append("title=%r" % title)
    if desc:
        bits.append("desc=%r" % desc)
    if pos is not None and size is not None:
        bits.append("@(%.0f,%.0f %.0fx%.0f)" % (pos[0], pos[1], size[0], size[1]))
    print("  " * depth + "  ".join(bits))
    if depth < maxdepth:
        for child in ax._attribute(el, AX.kAXChildrenAttribute) or []:
            walk(child, depth + 1, maxdepth)


def dump(label):
    print("\n" + "=" * 66)
    print("== %s" % label)
    group = ax.mission_control_group()
    if group is None:
        print("   (no mc group!)")
        return
    walk(group)
    sys.stdout.flush()


def main():
    subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
    time.sleep(1.7)
    dump("MC OPENED FROM DESKTOP")

    key(124, Quartz.kCGEventFlagMaskControl)  # Ctrl+Right: pan to next space
    time.sleep(1.6)
    dump("AFTER PANNING RIGHT (should be viewing a fullscreen space)")

    key(124, Quartz.kCGEventFlagMaskControl)
    time.sleep(1.6)
    dump("AFTER PANNING RIGHT AGAIN")

    # go back and dismiss
    key(123, Quartz.kCGEventFlagMaskControl)
    time.sleep(0.8)
    key(123, Quartz.kCGEventFlagMaskControl)
    time.sleep(0.8)
    key(53)
    time.sleep(0.5)
    key(53)
    print("\ndone (dismissed)")


if __name__ == "__main__":
    main()
