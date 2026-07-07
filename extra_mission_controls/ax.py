"""Read the Dock's Mission Control AX tree and close other apps' windows
through the Accessibility (AX) API."""

import ApplicationServices as AX
import Quartz

_kAXValueCGPointType = getattr(AX, "kAXValueCGPointType", 1)
_kAXValueCGSizeType = getattr(AX, "kAXValueCGSizeType", 2)


def is_trusted(prompt=False):
    """True when this process may use the Accessibility API."""
    if prompt:
        options = {AX.kAXTrustedCheckOptionPrompt: True}
        return AX.AXIsProcessTrustedWithOptions(options)
    return AX.AXIsProcessTrusted()


def ensure_permissions():
    """Prompt for Accessibility and Screen Recording access if missing.
    Returns (ax_ok, capture_ok)."""
    ax_ok = is_trusted(prompt=True)
    capture_ok = bool(Quartz.CGPreflightScreenCaptureAccess())
    if not capture_ok:
        Quartz.CGRequestScreenCaptureAccess()
    return ax_ok, capture_ok


def _attribute(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == AX.kAXErrorSuccess else None


def _ax_windows(pid):
    app = AX.AXUIElementCreateApplication(pid)
    # Don't let one unresponsive app stall us for the default 6 seconds.
    AX.AXUIElementSetMessagingTimeout(app, 0.5)
    return list(_attribute(app, AX.kAXWindowsAttribute) or [])


def _unpack_point(ax_value):
    if ax_value is None:
        return None
    try:
        ok, point = AX.AXValueGetValue(ax_value, _kAXValueCGPointType, None)
        return (point.x, point.y) if ok else None
    except Exception:
        return None


def _unpack_size(ax_value):
    if ax_value is None:
        return None
    try:
        ok, size = AX.AXValueGetValue(ax_value, _kAXValueCGSizeType, None)
        return (size.width, size.height) if ok else None
    except Exception:
        return None


_dock_pid_cache = None


def _dock_pid():
    global _dock_pid_cache
    if _dock_pid_cache is not None:
        return _dock_pid_cache
    from AppKit import NSWorkspace
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == "com.apple.dock":
            _dock_pid_cache = app.processIdentifier()
            return _dock_pid_cache
    return None


def mission_control_probably_active():
    """Cheap idle-poll prefilter: ONE WindowServer query, no AX IPC and no
    app wake-ups. While Mission Control is up the Dock owns fullscreen
    windows at layer 18-20; quiescent it only owns wallpaper/backdrop layers
    (negative) and the dock bar itself. Callers confirm a positive with
    mission_control_group() — this only exists to make the idle poll free."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for entry in raw:
        if entry.get(Quartz.kCGWindowOwnerName) == "Dock" \
                and 18 <= entry.get(Quartz.kCGWindowLayer, 0) <= 20:
            return True
    return False


def mission_control_group():
    """The Dock's 'mc' AX group — present only while Mission Control is up."""
    pid = _dock_pid()
    if pid is None:
        return None
    dock = AX.AXUIElementCreateApplication(pid)
    for child in _attribute(dock, AX.kAXChildrenAttribute) or []:
        if _attribute(child, "AXIdentifier") == "mc":
            return child
    return None


def mission_control_thumbnails(group=None):
    """Window thumbnails Mission Control is currently showing, as dicts with
    title/x/y/width/height (top-left-origin global coordinates).

    Dock AX layout on macOS 26: mc → mc.display → mc.windows → AXButton per
    thumbnail. Undocumented; may shift across macOS releases."""
    if group is None:
        group = mission_control_group()
    if group is None:
        return []
    thumbs = []
    for display in _attribute(group, AX.kAXChildrenAttribute) or []:
        if _attribute(display, "AXIdentifier") != "mc.display":
            continue
        for section in _attribute(display, AX.kAXChildrenAttribute) or []:
            if _attribute(section, "AXIdentifier") != "mc.windows":
                continue
            for button in _attribute(section, AX.kAXChildrenAttribute) or []:
                position = _unpack_point(
                    _attribute(button, AX.kAXPositionAttribute))
                size = _unpack_size(_attribute(button, AX.kAXSizeAttribute))
                if position is None or size is None:
                    continue
                thumbs.append({
                    "title": _attribute(button, AX.kAXTitleAttribute) or "",
                    "x": position[0],
                    "y": position[1],
                    "width": size[0],
                    "height": size[1],
                    # AXPress focuses the window (and exits Mission Control).
                    "element": button,
                })
    return thumbs


def mission_control_window_count(group=None):
    """Number of children in the mc.windows section, readable or not.

    Zero means an empty desktop overview. A positive count paired with no
    readable thumbnails means a fullscreen/split space is being viewed: its
    window appears as a child with no usable AX frame, and in that state the
    Spaces Bar reports stale (desktop-overview) coordinates."""
    if group is None:
        group = mission_control_group()
    if group is None:
        return 0
    for display in _attribute(group, AX.kAXChildrenAttribute) or []:
        if _attribute(display, "AXIdentifier") != "mc.display":
            continue
        for section in _attribute(display, AX.kAXChildrenAttribute) or []:
            if _attribute(section, "AXIdentifier") == "mc.windows":
                return len(_attribute(section, AX.kAXChildrenAttribute) or [])
    return 0


def describe_mc_tree(group, max_depth=4):
    """Structural dump of the Mission Control subtree (role/subrole/id/frame/
    child-count per node) — used to find what distinguishes an empty desktop
    overview from a fullscreen-space view when neither shows window thumbnails."""
    lines = []

    def walk(element, depth):
        role = _attribute(element, AX.kAXRoleAttribute)
        ident = _attribute(element, "AXIdentifier")
        sub = _attribute(element, AX.kAXSubroleAttribute)
        title = _attribute(element, AX.kAXTitleAttribute)
        pos = _unpack_point(_attribute(element, AX.kAXPositionAttribute))
        size = _unpack_size(_attribute(element, AX.kAXSizeAttribute))
        children = _attribute(element, AX.kAXChildrenAttribute) or []
        frame = ("@(%.0f,%.0f %.0fx%.0f)" % (pos[0], pos[1], size[0], size[1])
                 if pos is not None and size is not None else "")
        lines.append("    %s%s id=%s sub=%s title=%r %s children=%d"
                     % ("  " * depth, role, ident, sub, title, frame,
                        len(children)))
        if depth < max_depth:
            for child in children:
                walk(child, depth + 1)

    if group is not None:
        walk(group, 0)
    return lines


def mission_control_spaces(group=None):
    """Space tiles in Mission Control's Spaces Bar (mc.spaces.list), as dicts
    with title/desc/x/y/width/height. Fullscreen apps appear here with the
    app's name as the title."""
    if group is None:
        group = mission_control_group()
    if group is None:
        return []
    spaces = []
    for display in _attribute(group, AX.kAXChildrenAttribute) or []:
        if _attribute(display, "AXIdentifier") != "mc.display":
            continue
        for section in _attribute(display, AX.kAXChildrenAttribute) or []:
            if _attribute(section, "AXIdentifier") != "mc.spaces":
                continue
            for child in _attribute(section, AX.kAXChildrenAttribute) or []:
                if _attribute(child, "AXIdentifier") != "mc.spaces.list":
                    continue
                for button in _attribute(child, AX.kAXChildrenAttribute) or []:
                    position = _unpack_point(
                        _attribute(button, AX.kAXPositionAttribute))
                    size = _unpack_size(
                        _attribute(button, AX.kAXSizeAttribute))
                    if position is None or size is None:
                        continue
                    spaces.append({
                        "title": _attribute(button, AX.kAXTitleAttribute) or "",
                        "desc": _attribute(
                            button, AX.kAXDescriptionAttribute) or "",
                        "x": position[0],
                        "y": position[1],
                        "width": size[0],
                        "height": size[1],
                        # AXPress on this element switches to the space.
                        "element": button,
                    })
    return spaces


def _pids_named(app_name):
    from AppKit import NSWorkspace
    return [app.processIdentifier()
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            if app.localizedName() == app_name]


def find_fullscreen_window(app_name):
    """The first fullscreen AX window of the app with this name, or None.
    AX enumerates windows on all spaces, so this works from any space."""
    for pid in _pids_named(app_name):
        for window in _ax_windows(pid):
            if _attribute(window, "AXFullScreen"):
                return window
    return None


def find_fullscreen_window_by_title(window_title):
    """A fullscreen window whose AX *window title* matches. Needed because the
    Dock retitles a fullscreen space by its window title after a split
    partner closes (e.g. 'Downloads' for a Finder window, 'All iCloud' for
    Notes) — those tile titles match no app name."""
    if not window_title:
        return None
    from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != NSApplicationActivationPolicyRegular:
            continue
        for window in _ax_windows(app.processIdentifier()):
            if _attribute(window, "AXFullScreen") and \
                    (_attribute(window, AX.kAXTitleAttribute) or "") == window_title:
                return window
    return None


def find_fullscreen_target(tile_title):
    """Resolve a Spaces Bar tile title to its fullscreen window: by app name
    first (normal fullscreen tiles), then by window title (retitled tiles)."""
    return (find_fullscreen_window(tile_title)
            or find_fullscreen_window_by_title(tile_title))


def window_frame(window):
    """(x, y, width, height) of a window element, top-left coords, or None."""
    position = _unpack_point(_attribute(window, AX.kAXPositionAttribute))
    size = _unpack_size(_attribute(window, AX.kAXSizeAttribute))
    if position is None or size is None:
        return None
    return (position[0], position[1], size[0], size[1])


def focus_window(window):
    """Raise the window, make it main, and bring its app frontmost. Split
    View halves often no-op their close button until they are focused."""
    AX.AXUIElementSetAttributeValue(window, AX.kAXMainAttribute, True)
    AX.AXUIElementPerformAction(window, AX.kAXRaiseAction)
    err, pid = AX.AXUIElementGetPid(window, None)
    if err == AX.kAXErrorSuccess and pid:
        from AppKit import (NSApplicationActivateIgnoringOtherApps,
                            NSRunningApplication)
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def running_app_names():
    """Localized names of all regular (Dock-visible) running apps."""
    from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace
    return {app.localizedName()
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            if app.activationPolicy() == NSApplicationActivationPolicyRegular
            and app.localizedName()}


def scan_closable_windows():
    """One AX sweep over all regular running apps. Returns
    (fullscreen_by_app, visible_app_names, windows_by_pid) where:

    - fullscreen_by_app: {app_name: (window_el, close_button_el, ax_title)}
    - visible_app_names: apps that reported any AX windows at all
    - windows_by_pid: {pid: [(ax_title, close_button_element), ...]}

    Must be called while Mission Control is CLOSED: apps stop reporting
    their AX windows entirely while it is open, and most (Notes, Steam,
    TextEdit, ...) report only current-space windows even normally. The
    returned element references stay pressable during Mission Control and
    from other spaces, so callers cache them."""
    from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace
    fullscreen = {}
    visible = set()
    by_pid = {}
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != NSApplicationActivationPolicyRegular:
            continue
        name = app.localizedName()
        if not name:
            continue
        pid = app.processIdentifier()
        ax_windows = _ax_windows(pid)
        if ax_windows:
            visible.add(name)
        entries = []
        for window in ax_windows:
            close_button = _attribute(window, AX.kAXCloseButtonAttribute)
            if close_button is None:
                continue
            title = _attribute(window, AX.kAXTitleAttribute) or ""
            entries.append((title, close_button))
            if name not in fullscreen and _attribute(window, "AXFullScreen"):
                fullscreen[name] = (window, close_button, title)
        if entries:
            by_pid[pid] = entries
    return fullscreen, visible, by_pid


def press_element(element):
    return AX.AXUIElementPerformAction(
        element, AX.kAXPressAction) == AX.kAXErrorSuccess


def remove_space(element):
    """AXRemoveDesktop on a Spaces Bar tile. For a fullscreen app's tile the
    Dock exits fullscreen (same as the native hover button) — Mission Control
    stays open and no AX access to the app itself is needed."""
    return AX.AXUIElementPerformAction(
        element, "AXRemoveDesktop") == AX.kAXErrorSuccess


def element_alive(element):
    """True while the AX element still refers to an existing UI object."""
    return element is not None and \
        _attribute(element, AX.kAXRoleAttribute) is not None


def close_button_of(window):
    """Fresh read of the window's close button — traffic-light buttons are
    recreated on fullscreen transitions, so held button refs go stale."""
    return _attribute(window, AX.kAXCloseButtonAttribute)


def find_app_window(app_name, held_window=None, title=None):
    """Re-find a window of the app after its element may have been recreated:
    by element identity first, then by AX title."""
    fallback = None
    for pid in _pids_named(app_name):
        for window in _ax_windows(pid):
            if held_window is not None and window == held_window:
                return window
            if title and fallback is None \
                    and _attribute(window, AX.kAXTitleAttribute) == title:
                fallback = window
    return fallback


def set_fullscreen(window, flag):
    return AX.AXUIElementSetAttributeValue(
        window, "AXFullScreen", flag) == AX.kAXErrorSuccess


def close_main_window(app_name):
    """Close the app's main/focused window. Used after un-fullscreening a
    window whose close button was hidden while it was fullscreen."""
    for pid in _pids_named(app_name):
        app = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetMessagingTimeout(app, 0.5)
        window = (_attribute(app, "AXMainWindow")
                  or _attribute(app, "AXFocusedWindow"))
        if window is None:
            continue
        close_button = _attribute(window, AX.kAXCloseButtonAttribute)
        if close_button is not None and AX.AXUIElementPerformAction(
                close_button, AX.kAXPressAction) == AX.kAXErrorSuccess:
            return True
    return False


def close_fullscreen_window(tile_title):
    """Close the fullscreen window behind a Spaces Bar tile title (matched by
    app name, then by window title); its space disappears with it."""
    window = find_fullscreen_target(tile_title)
    if window is None:
        return False
    close_button = _attribute(window, AX.kAXCloseButtonAttribute)
    if close_button is None:
        return False
    return AX.AXUIElementPerformAction(
        close_button, AX.kAXPressAction) == AX.kAXErrorSuccess


def close_window_by_title(title, pids):
    """Find a window titled `title` among the given pids and press its close
    button. Used to close windows behind Mission Control thumbnails."""
    if not title:
        return False
    for pid in pids:
        for window in _ax_windows(pid):
            if _attribute(window, AX.kAXTitleAttribute) != title:
                continue
            close_button = _attribute(window, AX.kAXCloseButtonAttribute)
            if close_button is None:
                continue
            if AX.AXUIElementPerformAction(
                    close_button, AX.kAXPressAction) == AX.kAXErrorSuccess:
                return True
    return False


