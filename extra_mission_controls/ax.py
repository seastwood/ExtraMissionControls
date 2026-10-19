"""Read the Dock's Mission Control AX tree and close other apps' windows
through the Accessibility (AX) API."""

import time

import ApplicationServices as AX
import Quartz

_kAXValueCGPointType = getattr(AX, "kAXValueCGPointType", 1)
# Debug-only extras (EMC_DEBUG=1): extra AX reads used purely for logging.
_DEBUG = bool(__import__("os").environ.get("EMC_DEBUG"))
_kAXValueCGSizeType = getattr(AX, "kAXValueCGSizeType", 2)


def is_trusted(prompt=False):
    """True when this process may use the Accessibility API."""
    if prompt:
        options = {AX.kAXTrustedCheckOptionPrompt: True}
        return AX.AXIsProcessTrustedWithOptions(options)
    return AX.AXIsProcessTrusted()


def ensure_permissions():
    """Prompt for Accessibility access if missing; returns whether it's
    granted. EMC deliberately does NOT ask for Screen Recording: it only ever
    unlocked other apps' window titles, and thumbnails are now matched to
    windows by bundle id and AX instead."""
    return is_trusted(prompt=True)


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


_pid_cache = {}


def _bundle_pid(bundle_id):
    """PID of a running system process by bundle id (cached; re-resolved by
    _forget_pid when its AX element stops answering, e.g. after a respawn)."""
    pid = _pid_cache.get(bundle_id)
    if pid is not None:
        return pid
    from AppKit import NSWorkspace
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == bundle_id:
            _pid_cache[bundle_id] = app.processIdentifier()
            return _pid_cache[bundle_id]
    return None


def _forget_pid(bundle_id):
    _pid_cache.pop(bundle_id, None)


def _dock_pid():
    return _bundle_pid(_DOCK)


_DOCK = "com.apple.dock"
# macOS 27 moved Mission Control's AX tree (and its overview windows) out of
# the Dock into WindowManager. The Dock keeps an EMPTY 'mc' stub group there.
_WINDOW_MANAGER = "com.apple.WindowManager"
_MC_WINDOW_OWNERS = ("Dock", "WindowManager")


def _macos_major():
    """Major macOS version, or 0 when it can't be told (a binary linked
    against an old SDK is shown the 10.16/16.0 compatibility version)."""
    import platform
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        major = 0
    try:
        from Foundation import NSProcessInfo
        major = max(major, int(
            NSProcessInfo.processInfo().operatingSystemVersion().majorVersion))
    except Exception:
        pass
    return 0 if major in (10, 16) else major


def _owner_layer_counts(raw):
    """Counter of (owner, layer) over the Dock's/WindowManager's on-screen
    windows in one CGWindowList result."""
    counts = {}
    for entry in raw:
        owner = entry.get(Quartz.kCGWindowOwnerName)
        if owner in _MC_WINDOW_OWNERS:
            key = (owner, entry.get(Quartz.kCGWindowLayer, 0))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _legacy_rule(counts):
    """The verified macOS 13-26 rule: a layer-18 backdrop window, or several
    in layers 18-20. One resident full-screen layer-20 window exists
    PERMANENTLY while any fullscreen space does (observed live: name='Dock',
    1470x956, alpha 1.0, with Mission Control closed), so mere presence in
    18-20 is NOT 'MC is up'. Mission Control itself adds more: a layer-18
    window plus another at 20 (observed: {20} idle vs {18, 20, 20} during
    MC)."""
    in_band = 0
    for (owner, layer), n in counts.items():
        if owner == "Dock" and 18 <= layer <= 20:
            if layer == 18:
                return True
            in_band += n
    return in_band >= 2


class _BackdropRule:
    """Decides 'is Mission Control's backdrop up?' from ONE WindowServer
    query, so the idle poll never has to wake the Dock or WindowManager.

    macOS 13-26: the verified _legacy_rule. macOS 27 moved the overview into
    WindowManager and its backdrop windows aren't documented, so there the
    rule is LEARNED: while unknown, the idle poll asks the AX tree directly
    (and records the Dock/WindowManager window counts as an idle baseline);
    the first AX-confirmed Mission Control session diffs its window counts
    against that baseline, and the windows that appeared become the rule.
    The rule is checked against every later confirmed session, re-learned if
    it misses, dropped if it fires repeatedly with no Mission Control, and
    stored per OS build once it has held twice, so later launches start
    cheap. A learned rule also gets a slow AX fallback check, so a rule gone
    wrong costs at most a short delay before it is re-learned."""

    FALLBACK_INTERVAL = 2.0     # learned mode: AX safety-net poll
    MAX_FALSE_HITS = 12         # rule fired with no MC this many idle polls
    HOLDS_TO_PERSIST = 2
    DEFAULTS_KEY = "MissionControlBackdropRule"

    def __init__(self):
        major = _macos_major()
        self.mode = "legacy" if 13 <= major <= 26 else "unknown"
        self.rule = []          # [(owner, layer, min_count)] — any one fires
        self.holds = 0
        self.false_hits = 0
        self.baseline = {}
        self.sample = {}
        self.last_fallback = 0.0
        self.rule_fired = False
        if self.mode == "unknown":
            self._load()

    # -- persistence ---------------------------------------------------------

    @staticmethod
    def _os_build():
        try:
            from Foundation import NSProcessInfo
            return str(NSProcessInfo.processInfo().operatingSystemVersionString())
        except Exception:
            return ""

    def _defaults(self):
        try:
            from Foundation import NSUserDefaults
            return NSUserDefaults.standardUserDefaults()
        except Exception:
            return None

    def _load(self):
        defaults = self._defaults()
        stored = defaults.dictionaryForKey_(self.DEFAULTS_KEY) if defaults else None
        if not stored or stored.get("os") != self._os_build():
            return
        try:
            rule = [(str(o), int(l), int(n)) for o, l, n in stored.get("rule") or []]
        except (TypeError, ValueError):
            return
        if rule:
            self.mode, self.rule, self.holds = "learned", rule, self.HOLDS_TO_PERSIST

    def _save(self):
        defaults = self._defaults()
        if defaults is not None:
            defaults.setObject_forKey_(
                {"os": self._os_build(),
                 "rule": [[o, l, n] for o, l, n in self.rule]},
                self.DEFAULTS_KEY)

    def _forget(self):
        self.mode, self.rule, self.holds, self.false_hits = "unknown", [], 0, 0
        defaults = self._defaults()
        if defaults is not None:
            defaults.removeObjectForKey_(self.DEFAULTS_KEY)

    # -- evaluation ----------------------------------------------------------

    def _learned_fires(self, counts):
        return any(counts.get((o, l), 0) >= n for o, l, n in self.rule)

    def present(self, counts):
        """For the per-frame pulse while Mission Control is up. Only the
        verified legacy rule may end a session early; otherwise report
        present and let the AX liveness check (mission_control_alive) end it."""
        if self.mode == "legacy":
            return _legacy_rule(counts)
        return True

    def idle_should_check(self, counts, now):
        """Idle poll: True when the AX tree is worth asking."""
        self.rule_fired = False
        self.sample = counts
        if self.mode == "legacy":
            return _legacy_rule(counts)
        if self.mode == "learned":
            if self._learned_fires(counts):
                self.rule_fired = True
                return True
            if now - self.last_fallback >= self.FALLBACK_INTERVAL:
                self.last_fallback = now
                return True
            return False
        return True

    def idle_check_found_nothing(self):
        # The AX tree just confirmed Mission Control is closed: this tick's
        # window counts are a clean idle baseline to diff a session against.
        self.baseline = self.sample
        if self.mode == "learned" and self.rule_fired:
            self.false_hits += 1
            if self.false_hits >= self.MAX_FALSE_HITS:
                self._forget()

    def confirmed(self, counts):
        """Mission Control confirmed up by the AX tree, `counts` taken now."""
        if self.mode == "legacy":
            return
        self.false_hits = 0
        if self.mode == "learned" and self._learned_fires(counts):
            self.holds += 1
            if self.holds == self.HOLDS_TO_PERSIST:
                self._save()
            return
        rule = [(o, l, self.baseline.get((o, l), 0) + 1)
                for (o, l), n in counts.items()
                if n > self.baseline.get((o, l), 0)]
        if rule:
            self.mode, self.rule, self.holds = "learned", rule, 1
        else:
            self._forget()


_backdrop = _BackdropRule()


def _window_list():
    return Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []


def backdrop_rule_mode():
    return _backdrop.mode


def mission_control_probably_active():
    """Whether Mission Control's backdrop looks up, from ONE WindowServer
    query (debug tracing only; the idle poll uses poll_mission_control)."""
    return _backdrop.present(_owner_layer_counts(_window_list()))


def poll_mission_control():
    """The idle detection poll: the Mission Control tree root when Mission
    Control is up, else None. Costs one WindowServer query; the Dock /
    WindowManager are only asked when the backdrop rule says it's worth it."""
    counts = _owner_layer_counts(_window_list())
    if not _backdrop.idle_should_check(counts, time.time()):
        return None
    group = mission_control_group()
    if group is None:
        _backdrop.idle_check_found_nothing()
    else:
        _backdrop.confirmed(counts)
    return group


# On-screen window ids from the latest pulse (the frame's one window-list
# pass), used to drop thumbnails of windows on OTHER spaces — see
# _current_space_only.
_onscreen_ids = frozenset()
_onscreen_at = 0.0


# System processes whose layer-0 windows are never app windows.
_PULSE_SKIP_OWNERS = {"Dock", "WindowManager", "Window Server", "Control Center",
                      "Control Centre", "Notification Center",
                      "Notification Centre", "Spotlight", "Wallpaper",
                      "screencapture", "TextInputMenuAgent", "loginwindow"}
pulse_owners = {}  # debug: window number -> (owner, w, h)


def small_system_windows():
    """Debug: on-screen windows owned by system processes (any layer) that
    are small enough to be a control — (owner, layer, x, y, w, h, alpha)."""
    out = []
    for entry in Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        owner = entry.get(Quartz.kCGWindowOwnerName)
        if owner not in _PULSE_SKIP_OWNERS:
            continue
        b = entry.get(Quartz.kCGWindowBounds) or {}
        w, h = b.get("Width", 0), b.get("Height", 0)
        if w > 120 or h > 120:
            continue
        out.append((owner, entry.get(Quartz.kCGWindowLayer, 0),
                    round(b.get("X", 0)), round(b.get("Y", 0)), round(w), round(h),
                    round(float(entry.get(Quartz.kCGWindowAlpha, 1.0)), 2)))
    return sorted(out)


def mission_control_pulse(exclude_pid):
    """(present, real_window_sig, watch_ids) from ONE WindowServer query —
    the sync tick's per-frame heartbeat. `present` is the backdrop rule.
    `real_window_sig` is a rounded-bounds signature of the layer-0 app
    windows (excluding ours): macOS 26 moves the REAL windows through Mission
    Control's animations, and at a gesture COMMIT the AX tree snaps to the
    final layout while these windows visibly ease after it — so 'AX still but
    real windows moving' identifies the commit, cursor position be damned
    (the Spaces Bar only expands, and so only signals, when the cursor is in
    the top region). One list pass; no extra queries over the old check."""
    sig = []
    watch_ids = []
    onscreen = []
    band = {}
    for entry in _window_list():
        layer = entry.get(Quartz.kCGWindowLayer, 0)
        if layer == 0:
            if entry.get(Quartz.kCGWindowOwnerPID) == exclude_pid:
                continue
            number = entry.get(Quartz.kCGWindowNumber, 0)
            onscreen.append(number)  # every on-screen window, for filtering
            bounds = entry.get(Quartz.kCGWindowBounds) or {}
            owner = entry.get(Quartz.kCGWindowOwnerName)
            # Only real app windows: macOS 27 adds short-lived system windows
            # while you hover in Mission Control (title labels, Spaces Bar
            # previews), and counting them read as windows opening/closing,
            # which hid every button on each hover.
            if (owner in _PULSE_SKIP_OWNERS
                    or bounds.get("Width", 0) < 80 or bounds.get("Height", 0) < 80):
                continue
            if _DEBUG:
                pulse_owners[number] = (owner, round(bounds.get("Width", 0)),
                                        round(bounds.get("Height", 0)))
            sig.append((number,
                        round(bounds.get("X", 0)), round(bounds.get("Y", 0)),
                        round(bounds.get("Width", 0)),
                        round(bounds.get("Height", 0))))
            watch_ids.append(number)
        elif 18 <= layer <= 20:
            owner = entry.get(Quartz.kCGWindowOwnerName)
            if owner in _MC_WINDOW_OWNERS:
                watch_ids.append(entry.get(Quartz.kCGWindowNumber, 0))
                band[(owner, layer)] = band.get((owner, layer), 0) + 1
    global _onscreen_ids, _onscreen_at
    _onscreen_ids, _onscreen_at = frozenset(onscreen + watch_ids), time.time()
    # The legacy rule only looks at layers 18-20; other modes ignore counts.
    present = _backdrop.present(band)
    return present, tuple(sig), tuple(watch_ids)


def windows_alive_signature(ids):
    """Rounded-bounds signature of SPECIFIC windows by id — the settled-state
    watchdog. CGWindowListCreateDescriptionFromArray costs ~0.11ms for a
    handful of ids versus ~0.8ms+ for a full on-screen list pass with
    per-entry bridging, so the quiet sync tick can ask 'did anything I care
    about move or vanish?' nearly for free. A window that closed (e.g. the
    Mission Control backdrop at exit) simply drops out of the description,
    changing the signature."""
    if not ids:
        return ()
    raw = Quartz.CGWindowListCreateDescriptionFromArray(ids) or []
    sig = []
    for entry in raw:
        bounds = entry.get(Quartz.kCGWindowBounds)
        if bounds is None:
            continue
        # One bridged call for the whole rect instead of four dict reads —
        # PyObjC bridging, not the WindowServer, dominates this function.
        ok, rect = Quartz.CGRectMakeWithDictionaryRepresentation(bounds, None)
        if not ok:
            continue
        sig.append((entry.get(Quartz.kCGWindowNumber, 0),
                    round(rect.origin.x), round(rect.origin.y),
                    round(rect.size.width), round(rect.size.height)))
    return tuple(sig)


# The AX element mission_control_group() returns for a WindowManager-hosted
# tree (macOS 27): WindowManager's application element itself, whose children
# are the mc.display groups. Kept so mission_control_alive can tell it apart
# from the Dock's 'mc' group (the app element outlives Mission Control).
_wm_root = None


def _has_display(root):
    return any(_attribute(child, "AXIdentifier") == "mc.display"
               for child in _attribute(root, AX.kAXChildrenAttribute) or [])


def _dock_mc_group():
    pid = _dock_pid()
    if pid is None:
        return None
    dock = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(dock, 0.5)
    children = _attribute(dock, AX.kAXChildrenAttribute)
    if children is None:
        _forget_pid(_DOCK)
        return None
    for child in children:
        if _attribute(child, "AXIdentifier") == "mc":
            # macOS 27 leaves an empty 'mc' stub here; only a populated
            # group is the real tree.
            if _attribute(child, AX.kAXChildrenAttribute):
                return child
            return None
    return None


def _window_manager_root():
    pid = _bundle_pid(_WINDOW_MANAGER)
    if pid is None:
        return None
    if _wm_root is None or _wm_root_pid != pid:
        _set_wm_root(pid)
    if _attribute(_wm_root, AX.kAXChildrenAttribute) is None:
        _forget_pid(_WINDOW_MANAGER)
        return None
    return _wm_root if _has_display(_wm_root) else None


_wm_root_pid = None


def _set_wm_root(pid):
    global _wm_root, _wm_root_pid
    _wm_root = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(_wm_root, 0.5)
    _wm_root_pid = pid


_prefer_window_manager = _macos_major() >= 27


def mission_control_group():
    """The element whose children are Mission Control's mc.display groups —
    present only while Mission Control is up. Through macOS 26 that is the
    Dock's 'mc' group; on macOS 27 the tree lives in WindowManager, whose
    application element holds the mc.display groups directly."""
    global _prefer_window_manager
    lookups = [_dock_mc_group, _window_manager_root]
    if _prefer_window_manager:
        lookups.reverse()
    for lookup in lookups:
        group = lookup()
        if group is not None:
            # Ask the host that answered first next time.
            _prefer_window_manager = lookup is _window_manager_root
            return group
    return None


def mission_control_host(group):
    """'WindowManager' or 'Dock' — which process hosts this tree (logging)."""
    return "WindowManager" if group is not None and group is _wm_root else "Dock"


def mission_control_alive(group):
    """True while `group` (from mission_control_group) still shows Mission
    Control. WindowManager's app element never dies, so for it this means
    'still has an mc.display group' — one AX call while the display element
    seen last frame is still listed, a full scan only when it isn't."""
    if group is None:
        return False
    if group is _wm_root:
        children = _attribute(group, AX.kAXChildrenAttribute) or []
        if _last_display is not None and _last_display in children:
            return True
        return any(_attribute(child, "AXIdentifier") == "mc.display"
                   for child in children)
    return element_alive(group)


_last_display = None  # an mc.display element from the latest snapshot


def _displays(group):
    return [display for display in _attribute(group, AX.kAXChildrenAttribute) or []
            if _attribute(display, "AXIdentifier") == "mc.display"]


def _space_origin(group, position, size):
    """Spaces Bar tiles report their CENTER as AXPosition when WindowManager
    hosts the tree (macOS 27); the Dock reported the top-left corner."""
    if group is _wm_root:
        return (position[0] - size[0] / 2, position[1] - size[1] / 2)
    return position


# macOS 26's Dock reported a Spaces Bar tile as just its preview image
# (138x90 on a 1470x956 screen); macOS 27's WindowManager reports the whole
# tile (168x129), padding and name label included. Buttons laid out from that
# frame's corner covered macOS's own remove/close (X) button on the preview,
# so convert back to the preview: same width ratio, the screen's aspect,
# horizontally centred, offset down as measured.
_PREVIEW_WIDTH_RATIO = 138.0 / 168.0
_PREVIEW_TOP_SHARE = 0.44


def _space_preview_rect(group, x, y, width, height):
    if group is not _wm_root or width <= 0:
        return x, y, width, height
    from AppKit import NSScreen
    screens = NSScreen.screens()
    aspect = 956.0 / 1470.0
    if screens:
        # The preview has the shape of the display the tile is on (an iPad in
        # Sidecar is shaped differently from the Mac's own screen).
        primary_h = screens[0].frame().size.height
        cx, cy = x + width / 2.0, y + height / 2.0
        chosen = screens[0]
        for screen in screens:
            f = screen.frame()
            top = primary_h - (f.origin.y + f.size.height)
            if (f.origin.x <= cx <= f.origin.x + f.size.width
                    and top <= cy <= top + f.size.height):
                chosen = screen
                break
        frame = chosen.frame()
        if frame.size.width > 0:
            aspect = frame.size.height / frame.size.width
    pw = width * _PREVIEW_WIDTH_RATIO
    ph = min(height, pw * aspect)
    # The preview isn't flush with the tile's top: measured ~17pt down on a
    # 129pt tile with a 90pt preview, i.e. ~44% of the leftover height (the
    # rest is the name label below).
    return x + (width - pw) / 2.0, y + (height - ph) * _PREVIEW_TOP_SHARE, pw, ph


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


_THUMB_ATTRS = ["AXTitle", "AXPosition", "AXSize", "AXDescription", "AXIdentifier"]
_SPACE_ATTRS = ["AXTitle", "AXDescription", "AXPosition", "AXSize"]
# A display's children are classified and, for macOS 27's direct thumbnail
# buttons, read in the same single batched call.
_CHILD_ATTRS = ["AXIdentifier", "AXRole", "wid"] + _THUMB_ATTRS


def _string(value):
    return value if isinstance(value, str) else None


def _display_parts(display):
    """One pass over an mc.display: ([(thumb element, attrs)], [space tile
    elements]). macOS 26 nests thumbnails in an mc.windows group; macOS 27
    puts them directly under mc.display (identifier '<bundle id>.space.<n>')
    beside mc.spaces. One batched AX call per child either way."""
    direct, nested, tiles = [], None, []
    for child in _attribute(display, AX.kAXChildrenAttribute) or []:
        vals = _multi_attributes(child, _CHILD_ATTRS)
        ident = _string(vals["AXIdentifier"])
        if ident == "mc.windows":
            nested = [(button, _multi_attributes(button, _THUMB_ATTRS))
                      for button in _attribute(child, AX.kAXChildrenAttribute) or []]
        elif ident == "mc.spaces":
            for part in _attribute(child, AX.kAXChildrenAttribute) or []:
                if _attribute(part, "AXIdentifier") == "mc.spaces.list":
                    tiles = list(_attribute(part, AX.kAXChildrenAttribute) or [])
        elif not (ident or "").startswith("mc.") \
                and _string(vals["AXRole"]) == "AXButton":
            direct.append((child, vals))
    return (nested if nested is not None else direct), tiles


def _window_id(vals):
    try:
        return int(vals.get("wid"))
    except (TypeError, ValueError):
        return None


def _current_space_only(buttons):
    """macOS 27 lists the DESKTOP's thumbnails, at their desktop-overview
    positions, even while Mission Control is showing a fullscreen space —
    buttons drawn for them float over the wrong window. Its thumbnails carry
    their CGWindowID ('wid'), and only the viewed space's windows are on
    screen, so keep the thumbnails whose window is on screen. Applied only
    when the latest window list is fresh and holds at least one thumbnail's
    window (proof this macOS keeps real windows on screen during Mission
    Control); otherwise nothing is filtered."""
    if time.time() - _onscreen_at > 0.5:
        return buttons
    ids = [_window_id(vals) for _, vals in buttons]
    if not any(wid in _onscreen_ids for wid in ids if wid is not None):
        return buttons
    return [pair for pair, wid in zip(buttons, ids)
            if wid is None or wid in _onscreen_ids]


def _active_space_id():
    """The managed id of the space currently shown, via the private (but
    long-stable) CGSGetActiveSpace; None if unavailable."""
    global _cgs
    if _cgs is None:
        try:
            import ctypes
            lib = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            lib.CGSMainConnectionID.restype = ctypes.c_int
            lib.CGSGetActiveSpace.argtypes = [ctypes.c_int]
            lib.CGSGetActiveSpace.restype = ctypes.c_uint64
            _cgs = lib
        except Exception:
            _cgs = False
    if not _cgs:
        return None
    try:
        return int(_cgs.CGSGetActiveSpace(_cgs.CGSMainConnectionID())) or None
    except Exception:
        return None


_cgs = None


# Set once CGSGetActiveSpace's id has matched a thumbnail's '.space.<n>' —
# proof the two use the same numbering, so an empty desktop can be filtered
# strictly.
_space_ids_verified = False


def _split_thumb_id(ident):
    """'com.apple.Safari.space.3' -> ('com.apple.Safari', 3); macOS 27
    WindowManager names every thumbnail this way. (None, None) otherwise."""
    if not isinstance(ident, str) or ".space." not in ident:
        return None, None
    bundle, _, number = ident.rpartition(".space.")
    try:
        return bundle, int(number)
    except ValueError:
        return bundle, None


def _active_space_ids():
    """The CURRENT space id of every display (CGSCopyManagedDisplaySpaces).
    CGSGetActiveSpace only answers for the display with the menu-bar focus,
    so on its own it filtered out every window on a second display (an iPad
    in Sidecar). Falls back to {CGSGetActiveSpace()} when unavailable."""
    global _cgs_spaces
    ids = set()
    try:
        import ctypes
        import objc
        if _cgs_spaces is None:
            lib = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            lib.CGSMainConnectionID.restype = ctypes.c_int
            lib.CGSCopyManagedDisplaySpaces.argtypes = [ctypes.c_int]
            lib.CGSCopyManagedDisplaySpaces.restype = ctypes.c_void_p
            _cgs_spaces = lib
        if _cgs_spaces:
            ptr = _cgs_spaces.CGSCopyManagedDisplaySpaces(
                _cgs_spaces.CGSMainConnectionID())
            if ptr:
                displays = objc.objc_object(c_void_p=ptr)
                for display in displays:
                    current = display.get("Current Space") or {}
                    space_id = current.get("ManagedSpaceID") or current.get("id64")
                    if space_id:
                        ids.add(int(space_id))
    except Exception:
        _cgs_spaces = False
    if not ids:
        active = _active_space_id()
        if active is not None:
            ids.add(active)
    return ids


_cgs_spaces = None


def _drop_other_space_thumbs(thumbs, spaces):
    """macOS 27 lists the windows of EVERY full-screen space among the
    current desktop's thumbnails — stacked, invisible, at one centred frame
    (the 'ghost window'). Keep only the shown space's thumbnails: by the
    active space id when it matches any thumbnail, else by dropping spaces
    whose thumbnails are the full-screen apps named in the Spaces Bar."""
    global _space_ids_verified
    numbered = [t for t in thumbs if t.get("space") is not None]
    if not numbered:
        return thumbs
    active = _active_space_ids()
    if active and any(t["space"] in active for t in numbered):
        _space_ids_verified = True
        return [t for t in thumbs if t.get("space") is None
                or t["space"] in active]
    if active and _space_ids_verified:
        # The active ids have matched real thumbnails before, so they use the
        # right numbering: no match now means EMPTY desktops, whose overview
        # lists other desktops' windows — show none of them.
        return [t for t in thumbs if t.get("space") is None]
    fs_titles = {s["title"] for s in spaces
                 if s["desc"].startswith("exit to full screen")}
    by_space = {}
    for t in numbered:
        by_space.setdefault(t["space"], []).append(t)
    fs_spaces = {n for n, members in by_space.items()
                 if all(m["title"] in fs_titles for m in members)
                 and len(by_space) > 1}
    return [t for t in thumbs if t.get("space") not in fs_spaces]


# {window title: bundle id} for every Mission Control thumbnail seen (macOS
# 27), recorded before any space filter — lets a tile or thumbnail title be
# traced to its app without CG window titles.
fullscreen_bundles = {}


def _note_bundles(buttons):
    """Record {title: bundle} for every thumbnail BEFORE any space filter, so
    a full-screen tile's title can be traced to its app."""
    for _, vals in buttons:
        bundle, _space = _split_thumb_id(vals.get("AXIdentifier"))
        title = vals.get("AXTitle")
        if bundle and isinstance(title, str) and title.strip():
            fullscreen_bundles[title] = bundle


def pid_for_title(title):
    """Best-effort pid behind a Spaces Bar tile title: the thumbnail's
    bundle id, else an app with that name."""
    bundle = fullscreen_bundles.get(title)
    if bundle is None:
        for t, b in fullscreen_bundles.items():
            if titles_related(title, t):
                bundle = b
                break
    pid = pid_for_bundle(bundle) if bundle else None
    if pid is None:
        pids = _pids_named(title)
        pid = pids[0] if pids else None
    return pid


def fullscreen_window_of_pid(pid):
    for window in _ax_windows(pid):
        if _attribute(window, "AXFullScreen"):
            return window
    return None


def focused_window_of_pid(pid):
    app = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(app, 0.5)
    return (_attribute(app, "AXFocusedWindow")
            or _attribute(app, "AXMainWindow"))


def app_name_for_pid(pid):
    from AppKit import NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return app.localizedName() if app is not None else None


def pid_for_bundle(bundle_id):
    from AppKit import NSRunningApplication
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        bundle_id) or []
    return apps[0].processIdentifier() if apps else None


def _app_name_for_bundle(bundle_id):
    from AppKit import NSRunningApplication
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        bundle_id) or []
    for app in apps:
        if app.localizedName():
            return app.localizedName()
    return None


def mission_control_snapshot(group):
    """One walk of the Mission Control subtree returning
    (thumbnails, spaces, window_count) — everything a sync frame needs.

    Each element's attributes come from one batched AX call, and structure is
    read once per frame — AX IPC dominates CPU while Mission Control is open
    (the readers run at 10 Hz), and every call wakes the host process."""
    global _last_display
    thumbs, spaces, window_count = [], [], 0
    if group is None:
        return thumbs, spaces, window_count
    for display in _displays(group):
        _last_display = display
        buttons, tiles = _display_parts(display)
        _note_bundles(buttons)
        buttons = _current_space_only(buttons)
        window_count += len(buttons)
        for button, vals in buttons:
            position = _unpack_point(vals["AXPosition"])
            size = _unpack_size(vals["AXSize"])
            if position is None or size is None:
                continue
            title = vals["AXTitle"]
            if not (isinstance(title, str) and title.strip()):
                title = vals.get("AXDescription")
            bundle, space = _split_thumb_id(vals.get("AXIdentifier"))
            if not (isinstance(title, str) and title.strip()) and bundle:
                # An untitled window of a real app (Safari's Start Page) —
                # name it by its app; the window lookups below resolve an
                # app-name title to that app's lone untitled window.
                title = _app_name_for_bundle(bundle) or ""
            thumbs.append({
                "space": space,
                "bundle": bundle,
                "title": title if isinstance(title, str) else "",
                "x": position[0],
                "y": position[1],
                "width": size[0],
                "height": size[1],
                # AXPress focuses the window (and exits Mission Control).
                "element": button,
            })
        for button in tiles:
            vals = _multi_attributes(button, _SPACE_ATTRS)
            position = _unpack_point(vals["AXPosition"])
            size = _unpack_size(vals["AXSize"])
            if position is None or size is None:
                continue
            position = _space_origin(group, position, size)
            px, py, pw, ph = _space_preview_rect(group, position[0], position[1],
                                                 size[0], size[1])
            title = vals["AXTitle"]
            desc = vals["AXDescription"]
            spaces.append({
                "title": title if isinstance(title, str) else "",
                "desc": desc if isinstance(desc, str) else "",
                "x": px,
                "y": py,
                "width": pw,
                "height": ph,
                # AXPress on this element switches to the space.
                "element": button,
            })
    if group is _wm_root:
        before = len(thumbs)
        thumbs = _drop_other_space_thumbs(thumbs, spaces)
        window_count -= before - len(thumbs)
    return thumbs, spaces, window_count


def mission_control_thumbnails(group=None):
    """Window thumbnails Mission Control is currently showing, as dicts with
    title/x/y/width/height (top-left-origin global coordinates).

    Dock AX layout on macOS 26: mc → mc.display → mc.windows → AXButton per
    thumbnail; on macOS 27 WindowManager → mc.display → AXButton.
    Undocumented; may shift across macOS releases."""
    if group is None:
        group = mission_control_group()
    return mission_control_snapshot(group)[0]


def mission_control_window_count(group=None):
    """Number of window thumbnail elements, readable or not.

    Zero means an empty desktop overview. A positive count paired with no
    readable thumbnails means a fullscreen/split space is being viewed: its
    window appears as a child with no usable AX frame, and in that state the
    Spaces Bar reports stale (desktop-overview) coordinates."""
    if group is None:
        group = mission_control_group()
    if group is None:
        return 0
    return sum(len(_current_space_only(_display_parts(display)[0]))
               for display in _displays(group))


def _action_names(element):
    try:
        err, names = AX.AXUIElementCopyActionNames(element, None)
        return list(names or []) if err == AX.kAXErrorSuccess else []
    except Exception:
        return []


def describe_actions(element, depth=0):
    """Debug: an element's actions plus its children's (role, subrole,
    title/description, actions), two levels down."""
    parts = ["actions=%s" % _action_names(element)]
    if depth < 2:
        for child in _attribute(element, AX.kAXChildrenAttribute) or []:
            parts.append("[%s/%s %r %r %s]" % (
                _attribute(child, AX.kAXRoleAttribute),
                _attribute(child, AX.kAXSubroleAttribute),
                _attribute(child, AX.kAXTitleAttribute),
                _attribute(child, AX.kAXDescriptionAttribute),
                describe_actions(child, depth + 1)))
    return " ".join(parts)


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
        desc = _attribute(element, AX.kAXDescriptionAttribute)
        children = _attribute(element, AX.kAXChildrenAttribute) or []
        frame = ("@(%.0f,%.0f %.0fx%.0f)" % (pos[0], pos[1], size[0], size[1])
                 if pos is not None and size is not None else "")
        lines.append("    %s%s id=%s sub=%s title=%r desc=%r %s children=%d"
                     % ("  " * depth, role, ident, sub, title, desc, frame,
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
    return mission_control_snapshot(group)[1]


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


def _core_dock_notify(name):
    """Post a Dock notification through the private CoreDockSendNotification
    (what the Show Desktop gesture and hot corner use). Returns False if the
    symbol can't be found."""
    import ctypes
    import objc
    from Foundation import NSString
    for path in ("/System/Library/Frameworks/ApplicationServices.framework/"
                 "Frameworks/HIServices.framework/HIServices",
                 "/System/Library/Frameworks/ApplicationServices.framework/"
                 "ApplicationServices"):
        try:
            fn = ctypes.CDLL(path).CoreDockSendNotification
        except (OSError, AttributeError):
            continue
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        fn.restype = None
        fn(objc.pyobjc_id(NSString.stringWithString_(name)), None)
        return True
    return False


def _show_desktop_hotkey():
    """The user's Show Desktop shortcut (symbolic hot key 36, F11 by default)
    as (keycode, flags), or None when it's turned off."""
    from CoreFoundation import CFPreferencesCopyAppValue
    keys = CFPreferencesCopyAppValue("AppleSymbolicHotKeys",
                                     "com.apple.symbolichotkeys") or {}
    entry = keys.get("36") or keys.get(36)
    if entry is None:
        return 103, 0x800000  # never customised: F11 (fn)
    if not entry.get("enabled", True):
        return None
    params = (entry.get("value") or {}).get("parameters") or ()
    if len(params) < 3:
        return None
    return int(params[1]), int(params[2])


def app_window_frames():
    """{window number: (x, y, w, h)} of on-screen app windows (layer 0). Show
    Desktop moves every one of them, so comparing two snapshots tells whether
    it took effect."""
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    frames = {}
    for w in info:
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        frames[w.get("kCGWindowNumber")] = (
            round(b.get("X", 0)), round(b.get("Y", 0)),
            round(b.get("Width", 0)), round(b.get("Height", 0)))
    return frames


def show_desktop(method="dock"):
    """Toggle Show Desktop. `method` 'dock' posts the Dock notification (falls
    back to the shortcut if unavailable); 'key' presses the user's Show Desktop
    shortcut. Returns True if something was sent."""
    if method == "dock":
        try:
            if _core_dock_notify("com.apple.showdesktop.awake"):
                return True
        except Exception:
            pass
    try:
        hotkey = _show_desktop_hotkey()
    except Exception:
        hotkey = (103, 0x800000)
    if hotkey is None:
        return False
    keycode, flags = hotkey
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return True


def close_all_windows(pid):
    """Press the close button of every window the app reports. Returns how
    many were pressed (apps report only the current space's windows)."""
    pressed = 0
    for window in _ax_windows(pid):
        button = _attribute(window, AX.kAXCloseButtonAttribute)
        if button is not None and AX.AXUIElementPerformAction(
                button, AX.kAXPressAction) == AX.kAXErrorSuccess:
            pressed += 1
    return pressed


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
        if ax_windows:
            # Re-read this app's windows from scratch (drop closed ones).
            for key in [k for k in window_caps if k[0] == pid]:
                window_caps.pop(key, None)
                caps_debug.pop(key, None)
        for window in ax_windows:
            close_button = _attribute(window, AX.kAXCloseButtonAttribute)
            if close_button is None:
                continue
            title = _attribute(window, AX.kAXTitleAttribute) or ""
            _note_caps(pid, title, window)
            minimize_button = _attribute(window, AX.kAXMinimizeButtonAttribute)
            entries.append((title, close_button, minimize_button))
            if name not in fullscreen and _attribute(window, "AXFullScreen"):
                fullscreen[name] = (window, close_button, title)
        if entries:
            by_pid[pid] = entries
    return fullscreen, visible, by_pid


# {(pid, window title): (can_fullscreen, can_maximize)} — read while Mission
# Control is closed (apps hide their AX windows while it is open), so
# thumbnails can drop buttons a window can't honour (System Settings has a
# disabled full-screen button and a fixed width).
window_caps = {}
caps_debug = {}


def _note_caps(pid, title, window):
    fs_button = _attribute(window, "AXFullScreenButton")
    can_fs = True
    if fs_button is not None:
        if _attribute(fs_button, "AXEnabled") is False:
            can_fs = False
    else:
        # No full-screen button at all (System Settings on macOS 27): it can
        # only go full screen if its AXFullScreen attribute is settable.
        try:
            err, settable = AX.AXUIElementIsAttributeSettable(
                window, "AXFullScreen", None)
            can_fs = bool(settable) if err == AX.kAXErrorSuccess else False
        except Exception:
            can_fs = False
    try:
        err, settable = AX.AXUIElementIsAttributeSettable(
            window, AX.kAXSizeAttribute, None)
        resizable = bool(settable) if err == AX.kAXErrorSuccess else True
    except Exception:
        resizable = True
    window_caps[(pid, title)] = (can_fs, resizable and can_fs)
    if not _DEBUG:
        return
    caps_debug[(pid, title)] = (
        fs_button is not None,
        _attribute(fs_button, "AXEnabled") if fs_button is not None else None,
        _attribute(fs_button, "AXSubrole") if fs_button is not None else None,
        resizable,
        _attribute(_attribute(window, "AXZoomButton"), "AXEnabled")
        if _attribute(window, "AXZoomButton") is not None else None)


def caps_for(pid, title):
    """(can_fullscreen, can_maximize) for a thumbnail, or (True, True) when
    unknown — never hide a button on a guess."""
    if pid is None:
        return True, True
    caps = window_caps.get((pid, title))
    if caps is not None:
        return caps
    related = [c for (p, t), c in window_caps.items()
               if p == pid and titles_related(title, t)]
    if len(related) == 1:
        return related[0]
    # Apps whose AX window title is blank (System Settings) can't be matched
    # by title; if the app has just one known window, it's that one.
    mine = [c for (p, _), c in window_caps.items() if p == pid]
    return mine[0] if len(mine) == 1 else (True, True)


def dock_app_items():
    """Running apps' Dock icons as dicts with title/x/y/width/height/bundle
    (top-left coords). The Dock still hosts its icons on macOS 27 (only
    Mission Control's tree moved to WindowManager)."""
    pid = _dock_pid()
    if pid is None:
        return []
    dock = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(dock, 0.5)
    items = []
    for child in _attribute(dock, AX.kAXChildrenAttribute) or []:
        if _attribute(child, AX.kAXRoleAttribute) != "AXList":
            continue
        for item in _attribute(child, AX.kAXChildrenAttribute) or []:
            vals = _multi_attributes(item, ["AXSubrole", "AXTitle", "AXPosition",
                                            "AXSize", "AXIsApplicationRunning",
                                            "AXURL"])
            if vals.get("AXSubrole") != "AXApplicationDockItem":
                continue
            running = vals.get("AXIsApplicationRunning")
            if not (isinstance(running, (bool, int)) and running):
                continue
            position = _unpack_point(vals.get("AXPosition"))
            size = _unpack_size(vals.get("AXSize"))
            if position is None or size is None:
                continue
            bundle = None
            try:
                from Foundation import NSBundle
                url = vals.get("AXURL")
                bundle_obj = NSBundle.bundleWithURL_(url) if url is not None else None
                bundle = bundle_obj.bundleIdentifier() if bundle_obj else None
            except Exception:
                bundle = None
            title = vals.get("AXTitle")
            items.append({"title": title if isinstance(title, str) else "",
                          "x": position[0], "y": position[1],
                          "width": size[0], "height": size[1],
                          "bundle": bundle})
    return items


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
    if target is None:
        window = _untitled_window_of(title, pids)
        target = _attribute(window, button_attr) if window is not None else None
    if target is None:
        window = _owner_window_for(title, button_attr)
        target = _attribute(window, button_attr) if window is not None else None
    return target is not None and AX.AXUIElementPerformAction(
        target, AX.kAXPressAction) == AX.kAXErrorSuccess


def _owner_window_for(title, button_attr=None):
    """Last-resort match through the thumbnail's own app (its bundle id from
    Mission Control), for apps whose AX window title differs from the title
    Mission Control shows — Safari's do (page vs window titles, a blank Start
    Page). The app's only window if it has one; else the single window whose
    title contains, or is contained in, the thumbnail's (case-insensitive)."""
    pid = pid_for_title(title)
    if pid is None:
        return None
    windows = [w for w in _ax_windows(pid)
               if button_attr is None or _attribute(w, button_attr) is not None]
    if len(windows) == 1:
        return windows[0]
    wanted = (title or "").strip().lower()
    if not wanted:
        return None
    hits = []
    for window in windows:
        have = (_attribute(window, AX.kAXTitleAttribute) or "").strip().lower()
        if have and (wanted in have or have in wanted):
            hits.append(window)
    return hits[0] if len(hits) == 1 else None


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
    if len(related) == 1:
        return related[0]
    return _untitled_window_of(title, pids) or _owner_window_for(title)


def _untitled_window_of(app_name, pids):
    """An untitled thumbnail is titled by its app's name (see
    mission_control_snapshot): resolve it to that app's ONLY untitled window
    among `pids`, or None when there isn't exactly one."""
    if not app_name:
        return None
    wanted = set(pids)
    found = []
    for pid in _pids_named(app_name):
        if wanted and pid not in wanted:
            continue
        for window in _ax_windows(pid):
            if not (_attribute(window, AX.kAXTitleAttribute) or "").strip():
                found.append(window)
    return found[0] if len(found) == 1 else None


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


