"""Dev tool: trigger Mission Control and dump the Dock's AX tree while it is
active, to discover what thumbnail elements macOS exposes. Run from the venv:

    .venv/bin/python scripts/probe_dock_ax.py
"""

import subprocess
import sys
import time

import ApplicationServices as AX
import Quartz
from AppKit import NSWorkspace


def dock_pid():
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == "com.apple.dock":
            return app.processIdentifier()
    raise SystemExit("Dock not running?")


def attr(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == AX.kAXErrorSuccess else None


def unpack(ax_value, ax_type):
    if ax_value is None:
        return None
    try:
        ok, value = AX.AXValueGetValue(ax_value, ax_type, None)
        return value if ok else None
    except Exception:
        return None


def describe(element):
    role = attr(element, AX.kAXRoleAttribute)
    subrole = attr(element, AX.kAXSubroleAttribute)
    title = attr(element, AX.kAXTitleAttribute)
    desc = attr(element, AX.kAXDescriptionAttribute)
    ident = attr(element, "AXIdentifier")
    pos = unpack(attr(element, AX.kAXPositionAttribute),
                 getattr(AX, "kAXValueCGPointType", 1))
    size = unpack(attr(element, AX.kAXSizeAttribute),
                  getattr(AX, "kAXValueCGSizeType", 2))
    parts = [str(role)]
    if subrole:
        parts.append("subrole=%s" % subrole)
    if title:
        parts.append("title=%r" % title)
    if desc:
        parts.append("desc=%r" % desc)
    if ident:
        parts.append("id=%r" % ident)
    if pos is not None:
        parts.append("pos=(%.0f,%.0f)" % (pos.x, pos.y))
    if size is not None:
        parts.append("size=(%.0f,%.0f)" % (size.width, size.height))
    err, actions = AX.AXUIElementCopyActionNames(element, None)
    if err == 0 and actions:
        parts.append("actions=%s" % list(actions))
    return "  ".join(parts)


def walk(element, depth=0, max_depth=8):
    print("%s%s" % ("  " * depth, describe(element)))
    if depth >= max_depth:
        return
    children = attr(element, AX.kAXChildrenAttribute) or []
    if len(children) > 40:
        print("%s  ... (%d children, showing first 40)" % ("  " * depth, len(children)))
    for child in list(children)[:40]:
        walk(child, depth + 1, max_depth)


def dump_dock(label):
    print("=" * 70)
    print("DOCK AX TREE — %s" % label)
    print("=" * 70)
    dock = AX.AXUIElementCreateApplication(dock_pid())
    walk(dock)
    sys.stdout.flush()


def dock_windows(label):
    print("-" * 70)
    print("DOCK-OWNED CG WINDOWS — %s" % label)
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for entry in raw:
        owner = entry.get(Quartz.kCGWindowOwnerName, "")
        if owner in ("Dock", "WindowManager"):
            print("  owner=%s layer=%s name=%r bounds=%s" % (
                owner,
                entry.get(Quartz.kCGWindowLayer),
                entry.get(Quartz.kCGWindowName, ""),
                dict(entry.get(Quartz.kCGWindowBounds) or {})))
    sys.stdout.flush()


def press_escape():
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 53, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def main():
    if not AX.AXIsProcessTrusted():
        raise SystemExit("Needs Accessibility permission.")

    dump_dock("quiescent (Mission Control closed)")
    dock_windows("quiescent")

    print("\n>>> Triggering Mission Control...\n")
    subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
    time.sleep(1.5)

    try:
        dump_dock("MISSION CONTROL ACTIVE")
        dock_windows("MISSION CONTROL ACTIVE")
    finally:
        press_escape()
        time.sleep(0.5)
        press_escape()  # belt and braces
        print("\n>>> Dismissed Mission Control.")


if __name__ == "__main__":
    main()
