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


def _mc_backdrop_windows():
    """The Dock's layer 18-20 windows: (layers, min_alpha). One resident
    full-screen layer-20 window exists PERMANENTLY while any fullscreen space
    does (observed live: name='Dock', 1470x956, alpha 1.0, with Mission
    Control closed), so mere presence in 18-20 is NOT 'MC is up'. Mission
    Control itself adds more: a layer-18 window plus another at 20 (observed:
    {20} idle vs {18, 20, 20} during MC)."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    layers = []
    min_alpha = 1.0
    for entry in raw:
        if entry.get(Quartz.kCGWindowOwnerName) == "Dock" \
                and 18 <= entry.get(Quartz.kCGWindowLayer, 0) <= 20:
            layers.append(entry.get(Quartz.kCGWindowLayer, 0))
            alpha = float(entry.get(Quartz.kCGWindowAlpha, 1.0))
            if alpha < min_alpha:
                min_alpha = alpha
    return layers, min_alpha


def mission_control_probably_active():
    """Cheap idle-poll prefilter: ONE WindowServer query, no AX IPC and no
    app wake-ups. True when the Dock's Mission Control backdrop is up — a
    layer-18 window, or several windows in 18-20 (a LONE layer-20 window is
    the resident fullscreen-space backdrop that exists even with MC closed).
    Callers confirm a positive with mission_control_group() — this only
    exists to make the idle poll free."""
    layers, _ = _mc_backdrop_windows()
    return 18 in layers or len(layers) >= 2


def mission_control_pulse(exclude_pid):
    """(present, real_window_sig) from ONE WindowServer query — the sync
    tick's per-frame heartbeat. `present` is the mission_control_probably_active
    rule. `real_window_sig` is a rounded-bounds signature of the layer-0 app
    windows (excluding ours): macOS 26 moves the REAL windows through Mission
    Control's animations, and at a gesture COMMIT the AX tree snaps to the
    final layout while these windows visibly ease after it — so 'AX still but
    real windows moving' identifies the commit, cursor position be damned
    (the Spaces Bar only expands, and so only signals, when the cursor is in
    the top region). One list pass; no extra queries over the old check."""
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    layers = []
    sig = []
    for entry in raw:
        layer = entry.get(Quartz.kCGWindowLayer, 0)
        if layer == 0:
            if entry.get(Quartz.kCGWindowOwnerPID) == exclude_pid:
                continue
            bounds = entry.get(Quartz.kCGWindowBounds) or {}
            sig.append((entry.get(Quartz.kCGWindowNumber, 0),
                        round(bounds.get("X", 0)), round(bounds.get("Y", 0)),
                        round(bounds.get("Width", 0)),
                        round(bounds.get("Height", 0))))
        elif entry.get(Quartz.kCGWindowOwnerName) == "Dock" \
                and 18 <= layer <= 20:
            layers.append(layer)
    present = 18 in layers or len(layers) >= 2
    return present, tuple(sig)


def mission_control_backdrop_state():
    """(present, settled) for Mission Control's backdrop, from the same single
    WindowServer query mission_control_probably_active uses.

    `present`: MC's own backdrop windows exist (same rule as
    mission_control_probably_active — a lone layer-20 window is the resident
    fullscreen-space backdrop, not MC). `settled`: every backdrop window sits
    at FULL alpha. The blur tracks a three-finger gesture's progress, so a
    partial entry/exit shows alpha < 1 — the one 'fully settled' signal for
    desktop transitions, where neither the AX tree (frozen at the settled
    layout throughout) nor the scene probe (desktop zooms don't transform
    overlay windows) sees anything move."""
    layers, min_alpha = _mc_backdrop_windows()
    present = 18 in layers or len(layers) >= 2
    return present, min_alpha >= 0.999


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


def _multi_attributes(element, names):
    """Read several attributes of one element with a SINGLE AX round-trip
    (AXUIElementCopyMultipleAttributeValues) instead of one Mach IPC per
    attribute. Returns {name: value}; missing attributes come back as
    error-typed AXValues, which the callers' unpack helpers turn into None."""
    try:
        err, values = AX.AXUIElementCopyMultipleAttributeValues(
            element, names, 0, None)
    except Exception:
        err, values = -1, None
    if err != AX.kAXErrorSuccess or values is None or len(values) != len(names):
        return {name: _attribute(element, name) for name in names}
    return dict(zip(names, values))


_THUMB_ATTRS = ["AXTitle", "AXPosition", "AXSize"]
_SPACE_ATTRS = ["AXTitle", "AXDescription", "AXPosition", "AXSize"]


def mission_control_snapshot(group):
    """One walk of the Mission Control subtree returning
    (thumbnails, spaces, window_count) — everything a sync frame needs.

    This replaces three separate walks (thumbnails, spaces, window count)
    that each re-read the same structural nodes, and fetches each leaf's
    attributes with one batched call instead of three or four — together
    roughly a 4x cut in AX IPC per frame, which dominates CPU while Mission
    Control is open (the readers run at 10 Hz)."""
    thumbs, spaces, window_count = [], [], 0
    if group is None:
        return thumbs, spaces, window_count
    for display in _attribute(group, AX.kAXChildrenAttribute) or []:
        if _attribute(display, "AXIdentifier") != "mc.display":
            continue
        for section in _attribute(display, AX.kAXChildrenAttribute) or []:
            ident = _attribute(section, "AXIdentifier")
            if ident == "mc.windows":
                children = _attribute(section, AX.kAXChildrenAttribute) or []
                window_count = len(children)
                for button in children:
                    vals = _multi_attributes(button, _THUMB_ATTRS)
                    position = _unpack_point(vals["AXPosition"])
                    size = _unpack_size(vals["AXSize"])
                    if position is None or size is None:
                        continue
                    title = vals["AXTitle"]
                    thumbs.append({
                        "title": title if isinstance(title, str) else "",
                        "x": position[0],
                        "y": position[1],
                        "width": size[0],
                        "height": size[1],
                        # AXPress focuses the window (and exits Mission Control).
                        "element": button,
                    })
            elif ident == "mc.spaces":
                for child in _attribute(section, AX.kAXChildrenAttribute) or []:
                    if _attribute(child, "AXIdentifier") != "mc.spaces.list":
                        continue
                    for button in _attribute(child, AX.kAXChildrenAttribute) or []:
                        vals = _multi_attributes(button, _SPACE_ATTRS)
                        position = _unpack_point(vals["AXPosition"])
                        size = _unpack_size(vals["AXSize"])
                        if position is None or size is None:
                            continue
                        title = vals["AXTitle"]
                        desc = vals["AXDescription"]
                        spaces.append({
                            "title": title if isinstance(title, str) else "",
                            "desc": desc if isinstance(desc, str) else "",
                            "x": position[0],
                            "y": position[1],
                            "width": size[0],
                            "height": size[1],
                            # AXPress on this element switches to the space.
                            "element": button,
                        })
    return thumbs, spaces, window_count


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
        activate_pid(pid)


def activate_pid(pid):
    """Bring the app with this pid frontmost. Needed before a synthetic click
    on a window that only accepts clicks while active (e.g. Steam's helper
    window, which exposes no AX close button)."""
    from AppKit import (NSApplicationActivateIgnoringOtherApps,
                        NSRunningApplication)
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        return True
    return False


def quit_pid(pid):
    """Ask the app with this pid to quit normally — the ⌘Q / 'Quit' path, so
    the app can prompt to save unsaved work (not a force kill). Returns True if
    the request was delivered."""
    from AppKit import NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        return bool(app.terminate())
    return False


def force_quit_pid(pid):
    """Force-quit the app with this pid — the Force Quit dialog's behaviour
    (forceTerminate). The app is killed without a chance to prompt or save.
    Returns True if the request was delivered; False if no NSRunningApplication
    resolves for the pid (e.g. a helper process — use kill_pid for those)."""
    from AppKit import NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        return bool(app.forceTerminate())
    return False


def kill_pid(pid):
    """Hardest stop: a raw SIGKILL straight to the process. Uncatchable and
    immediate, and it targets the exact pid (so it works even for the helper
    processes that own some apps' visible windows, e.g. Steam, where
    forceTerminate finds no NSRunningApplication). Returns True if the signal
    was sent."""
    import os
    import signal
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


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
    - windows_by_pid: {pid: [(ax_title, close_button_el, minimize_button_el),
      ...]} (minimize_button_el may be None)

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
            minimize_button = _attribute(window, AX.kAXMinimizeButtonAttribute)
            entries.append((title, close_button, minimize_button))
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


def fullscreen_window_of(app_name):
    """Put the app's main/focused window into full screen via its AX
    full-screen button (preferred — needs no pixel coordinates, so it works
    for apps like Claude whose traffic lights are inset off the standard
    position), else the AXFullScreen attribute. Returns True if the action was
    accepted; AX-opaque windows (Steam) expose neither, returning False so the
    caller can fall back to a synthetic green-button click. Verify separately —
    a window can report success without actually entering full screen."""
    for pid in _pids_named(app_name):
        app = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetMessagingTimeout(app, 0.5)
        window = (_attribute(app, "AXMainWindow")
                  or _attribute(app, "AXFocusedWindow"))
        if window is None:
            continue
        button = _attribute(window, "AXFullScreenButton")
        if button is not None:
            return AX.AXUIElementPerformAction(
                button, AX.kAXPressAction) == AX.kAXErrorSuccess
        return set_fullscreen(window, True)
    return False


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


def titles_related(a, b):
    """True when two window titles refer to the same window despite the Dock
    showing a shortened form for the Mission Control thumbnail — e.g. thumbnail
    'Songs' vs the window's AX title 'Songs – 1 note'. Exact match, or one
    title is a prefix of the other."""
    if a == b:
        return True
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def close_window_by_title(title, pids):
    """Find the window matching a Mission Control thumbnail title among the
    given pids and press its close button."""
    return _press_window_button_by_title(title, pids, AX.kAXCloseButtonAttribute)


def minimize_window_by_title(title, pids):
    """Find the window matching a thumbnail title among the given pids and
    press its minimize button (apps that report windows during MC)."""
    return _press_window_button_by_title(
        title, pids, AX.kAXMinimizeButtonAttribute)


def _press_window_button_by_title(title, pids, button_attr):
    """Press a window's close/minimize button, matching the thumbnail title to
    the live window: exact title first, else a UNIQUE prefix match (the Dock
    routinely shortens the title it shows on the thumbnail). Ambiguous prefix
    matches are ignored so the wrong window is never touched."""
    if not title:
        return False
    exact, related = [], []
    for pid in pids:
        for window in _ax_windows(pid):
            button = _attribute(window, button_attr)
            if button is None:
                continue
            window_title = _attribute(window, AX.kAXTitleAttribute) or ""
            if window_title == title:
                exact.append(button)
            elif titles_related(title, window_title):
                related.append(button)
    target = exact[0] if exact else (related[0] if len(related) == 1 else None)
    return target is not None and AX.AXUIElementPerformAction(
        target, AX.kAXPressAction) == AX.kAXErrorSuccess


def _find_window_by_title(title, pids):
    """The live window matching a thumbnail title among the given pids: exact
    title, else a UNIQUE prefix match (the Dock shortens the title it shows).
    Ambiguous prefixes return None so the wrong window is never touched — the
    same rule the close/minimize matching uses."""
    if not title:
        return None
    exact, related = [], []
    for pid in pids:
        for window in _ax_windows(pid):
            window_title = _attribute(window, AX.kAXTitleAttribute) or ""
            if window_title == title:
                exact.append(window)
            elif titles_related(title, window_title):
                related.append(window)
    if exact:
        return exact[0]
    return related[0] if len(related) == 1 else None


def _snap_frame(region, win_frame):
    """The AX (top-left origin) frame for a snap region of the screen the window
    currently sits on, using that screen's *visible* area so the window clears
    the menu bar and the Dock: the left half, the right half, or 'max' for the
    whole visible area. `win_frame` is the window's current (x, y, w, h); its
    centre picks the screen on a multi-display setup."""
    from AppKit import NSScreen
    screens = NSScreen.screens()
    if not screens:
        return None
    primary_h = screens[0].frame().size.height  # AX origin = primary top-left
    wx, wy, ww, wh = win_frame
    cx, cy = wx + ww / 2.0, wy + wh / 2.0

    def to_ax(rect):  # Cocoa (bottom-left) rect -> AX (top-left) rect
        return (rect.origin.x,
                primary_h - (rect.origin.y + rect.size.height),
                rect.size.width, rect.size.height)

    chosen = screens[0]
    for screen in screens:
        sx, sy, sw, sh = to_ax(screen.frame())
        if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
            chosen = screen
            break
    vx, vy, vw, vh = to_ax(chosen.visibleFrame())
    if region == "left":
        return (vx, vy, vw / 2.0, vh)
    if region == "right":
        return (vx + vw / 2.0, vy, vw / 2.0, vh)
    return (vx, vy, vw, vh)  # 'max' — whole visible area (not macOS full screen)


def snap_window_by_title(title, pids, region):
    """Move+resize the window matching a thumbnail title to a region of its
    screen's visible area: the left half, the right half, or 'max' (fill the
    whole visible area — maximize on the desktop, not macOS full screen). Window
    matched like close/minimize (exact title, else unique prefix). Best-effort:
    apps with a fixed or minimum window size clamp, and AX-opaque windows may
    ignore it. Returns True if the AX frame updates were accepted."""
    window = _find_window_by_title(title, pids)
    if window is None:
        return False
    frame = window_frame(window)
    if frame is None:
        return False
    target = _snap_frame(region, frame)
    if target is None:
        return False
    x, y, w, h = target
    pos = AX.AXValueCreate(_kAXValueCGPointType, Quartz.CGPointMake(x, y))
    size = AX.AXValueCreate(_kAXValueCGSizeType, Quartz.CGSizeMake(w, h))
    # Position, then size, then position again: setting one can be constrained
    # by the other (some apps re-centre on resize), so a second position pass
    # settles the window where we asked.
    r1 = AX.AXUIElementSetAttributeValue(window, AX.kAXPositionAttribute, pos)
    AX.AXUIElementSetAttributeValue(window, AX.kAXSizeAttribute, size)
    r2 = AX.AXUIElementSetAttributeValue(window, AX.kAXPositionAttribute, pos)
    return AX.kAXErrorSuccess in (r1, r2)


# Native macOS window tiling (macOS 15+) lives in the app's menu bar under
# Window ▸ Move & Resize ▸ {Left | Right | Fill}. Pressing the leaf item makes
# the window a REAL macOS tile — it joins the tile group and gets the shared
# resize divider between adjacent halves — unlike snap_window_by_title, which
# only sets a frame the window system doesn't recognise as a pair. Matched by
# English menu titles (other locales fall through to the raw snap), and only
# standard AppKit apps expose these items (Electron/Java/game windows don't), so
# tile_window_native returns False whenever it can't drive the menu.
_TILE_WINDOW_MENU = ("Window",)
_TILE_SUBMENU = ("Move & Resize",)
_TILE_LEAF = {"left": ("Left",), "right": ("Right",), "max": ("Fill", "Maximize")}


def _submenu_items(element):
    """The AXMenuItem children of a menu bar item or menu item: its submenu is
    the single AXMenu child, whose children are the items. Empty if the element
    has no (populated) submenu."""
    for child in _attribute(element, AX.kAXChildrenAttribute) or []:
        if _attribute(child, AX.kAXRoleAttribute) == "AXMenu":
            return list(_attribute(child, AX.kAXChildrenAttribute) or [])
    return []


def _menu_item_titled(items, titles):
    """The first menu item whose AX title is in `titles`, or None."""
    for item in items:
        if (_attribute(item, AX.kAXTitleAttribute) or "") in titles:
            return item
    return None


def focus_window_by_title(title, pids):
    """Make the window matching a thumbnail title the app's main window, raise
    it, and bring the app frontmost — so a subsequent native-tile menu press
    lands on it. Returns the owning pid, or None if the window wasn't found.

    Split from the menu press (tile_focused_app) on purpose: activation is
    asynchronous, so the caller waits a beat between focusing and pressing, and
    stages multiple windows apart so their tile animations don't collide."""
    window = _find_window_by_title(title, pids)
    if window is None:
        return None
    err, pid = AX.AXUIElementGetPid(window, None)
    if err != AX.kAXErrorSuccess or not pid:
        return None
    AX.AXUIElementSetAttributeValue(window, AX.kAXMainAttribute, True)
    AX.AXUIElementPerformAction(window, AX.kAXRaiseAction)
    activate_pid(pid)
    return pid


def tile_focused_app(pid, region):
    """Press the native tiling menu leaf for the app's focused window: Window ▸
    Move & Resize ▸ Left/Right, or the top-level Fill for 'max'. Returns True
    only if the item was found and the press succeeded; False otherwise (no such
    menu, wrong locale, disabled item), so the caller can fall back to a raw
    snap. Pair with focus_window_by_title, which makes the target window main."""
    leaf_titles = _TILE_LEAF.get(region)
    if not leaf_titles or not pid:
        return False
    app = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(app, 0.5)
    menu_bar = _attribute(app, "AXMenuBar")
    if menu_bar is None:
        return False
    window_menu = _menu_item_titled(
        _attribute(menu_bar, AX.kAXChildrenAttribute) or [], _TILE_WINDOW_MENU)
    if window_menu is None:
        return False
    items = _submenu_items(window_menu)
    # Sequoia/Tahoe nest the tile items under a "Move & Resize" submenu; look
    # there first, then fall back to any that sit directly in the Window menu.
    candidates = []
    submenu = _menu_item_titled(items, _TILE_SUBMENU)
    if submenu is not None:
        candidates.extend(_submenu_items(submenu))
    candidates.extend(items)
    leaf = _menu_item_titled(candidates, leaf_titles)
    if leaf is None:
        return False
    # Don't pre-check AXEnabled: right after activation the app may not have come
    # forward yet, so a genuinely tileable item can still read disabled. Just
    # press — AXPress returns non-success on a truly disabled item (e.g. an
    # Electron window that can't tile), and the caller then snaps.
    return press_element(leaf)


def tile_window_native(title, pids, region):
    """Single-shot native tile: focus the matching window, then press its tiling
    menu leaf. Returns True on success. The staged Arrange path uses
    focus_window_by_title + tile_focused_app directly so it can wait between the
    two steps; this convenience wrapper is for a one-off with no such need."""
    pid = focus_window_by_title(title, pids)
    if pid is None:
        return False
    return tile_focused_app(pid, region)


