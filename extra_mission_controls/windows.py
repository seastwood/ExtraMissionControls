"""Enumerate on-screen windows via the CG window list."""

import Quartz

# System processes that never own closable user windows.
_SKIP_OWNERS = {
    "Dock",
    "Window Server",
    "WindowManager",
    "Control Center",
    "Control Centre",
    "Notification Center",
    "Notification Centre",
    "Spotlight",
    "Wallpaper",
    "screencapture",
    "TextInputMenuAgent",
}

# Windows smaller than this (points) are palettes/tooltips, not real windows.
_MIN_DIMENSION = 80


class WindowInfo:
    """Snapshot of one on-screen window from CGWindowListCopyWindowInfo."""

    def __init__(self, window_id, pid, owner, title, x, y, width, height):
        self.window_id = window_id
        self.pid = pid
        self.owner = owner
        self.title = title
        self.x = x
        self.y = y
        self.width = width
        self.height = height

    @property
    def display_title(self):
        if self.title and self.title != self.owner:
            return "%s — %s" % (self.owner, self.title)
        return self.owner

    def __repr__(self):
        return "WindowInfo(id=%s, owner=%r, title=%r)" % (
            self.window_id, self.owner, self.title)


def list_windows(exclude_pid=None):
    """Return WindowInfo for every normal window on the current space,
    front-to-back order (same order Mission Control uses as its input)."""
    options = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    windows = []
    for entry in raw:
        if entry.get(Quartz.kCGWindowLayer, 0) != 0:
            continue  # layer 0 = normal document windows
        pid = entry.get(Quartz.kCGWindowOwnerPID)
        if exclude_pid is not None and pid == exclude_pid:
            continue
        owner = entry.get(Quartz.kCGWindowOwnerName, "") or ""
        if owner in _SKIP_OWNERS:
            continue
        bounds = entry.get(Quartz.kCGWindowBounds) or {}
        width = bounds.get("Width", 0)
        height = bounds.get("Height", 0)
        if width < _MIN_DIMENSION or height < _MIN_DIMENSION:
            continue
        windows.append(WindowInfo(
            window_id=entry.get(Quartz.kCGWindowNumber),
            pid=pid,
            owner=owner,
            title=entry.get(Quartz.kCGWindowName, "") or "",
            x=bounds.get("X", 0),
            y=bounds.get("Y", 0),
            width=width,
            height=height,
        ))
    return windows


