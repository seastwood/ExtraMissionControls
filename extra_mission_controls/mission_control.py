"""Float ✕ close buttons over the real Mission Control.

Two kinds of targets get a button:
- window thumbnails on the current space (mc.windows), and
- Spaces Bar tiles that belong to fullscreen apps (mc.spaces.list) —
  never plain "Desktop N" tiles.

The Dock exposes both layouts through the Accessibility API (see ax.py).
While Mission Control is active we park one tiny always-on-top panel over
each target's top-left corner, re-synced at 10 Hz so the buttons track the
Spaces Bar expanding/shrinking and thumbnails re-flowing.

Two macOS quirks shape the design here:

1. Input: while Mission Control is up, WindowServer routes mouse events to
   the Dock even though our panels render on top, so window hit-testing never
   sees clicks. A CGEventTap claims clicks on our button rects (only those)
   before the Dock gets them.

2. Fullscreen windows: apps stop reporting AX windows entirely while Mission
   Control is open, and most report only current-space windows even normally.
   So we keep an accumulative registry of fullscreen windows (scanned when
   Mission Control is closed, refreshed on every space switch) whose held
   AX references remain pressable from anywhere. For fullscreen spaces we
   never got a reference to, the ✕ falls back to switching to that space
   (AXPress on its tile) and closing the window once its app becomes visible."""

import os
import subprocess
import time
import traceback

import objc
import Quartz
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSPanel,
    NSPopUpMenuWindowLevel,
    NSScreen,
    NSTimer,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWorkspace,
    NSWorkspaceActiveSpaceDidChangeNotification,
)
from Foundation import NSMakeRect, NSObject
from PyObjCTools import AppHelper

from . import ax, ui, windows

_DETECT_INTERVAL = 0.25   # Mission Control open/close detection
_SYNC_INTERVAL = 0.1      # button re-positioning while MC is active
_HOVER_INTERVAL = 1 / 30.0  # mouse-position polling while MC is active
_REGISTRY_MIN_GAP = 1.0   # coalesce event-driven registry scans
_BUTTON_INSET = 4.0
_BUTTON_GAP = 5.0         # spacing between the traffic-light-style buttons
_MIN_BUTTON_SIZE = 14.0   # shrink ✕ on collapsed Spaces Bar tiles
_DEBUG = bool(os.environ.get("EMC_DEBUG"))
# When EMC_DEBUG is set, also append to this file so geometry can be captured
# while the user drives Mission Control (no screen takeover needed).
_LOG_FILE = os.environ.get("EMC_LOG",
                           "/tmp/extramissioncontrols.log" if _DEBUG else "")
# Layout geometry is always logged here (truncated each launch) so the
# misplaced-button bug can be diagnosed from an ordinary user repro.
_GEOMETRY_LOG = os.environ.get(
    "EMC_GEOMETRY_LOG", "/tmp/extramissioncontrols-geometry.log")

# The Spaces Bar is only in its stable, correctly-placeable state when its
# tiles are expanded to full height at a positive y. While Mission Control
# opens, or while you pan into / view a fullscreen or split space, the bar
# collapses to a thin strip (~24px tall) that slides above the screen top
# (negative y) and the window thumbnails animate toward the center. Placing
# buttons in that state puts them in the wrong spot and they can't be clicked,
# so we suppress all buttons until the bar settles.
_MIN_BAR_TILE_HEIGHT = 50.0


def _debug(message):
    if not _DEBUG:
        return
    print("EMC:", message, flush=True)
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a") as fh:
                fh.write(message + "\n")
        except OSError:
            pass


def _geom_log(message):
    """Diagnostic trace (close chains, layouts) — debug builds only."""
    if not _DEBUG:
        return
    try:
        with open(_GEOMETRY_LOG, "a") as fh:
            fh.write(message + "\n")
    except OSError:
        pass


class MissionControlButtons(NSObject):
    """Polls the Dock for Mission Control and manages the ✕ button panels."""

    def init(self):
        self = objc.super(MissionControlButtons, self).init()
        if self is None:
            return None
        self._panels = []           # reusable pool, one per target slot
        self._buttons = []          # the CloseButton inside each panel
        self._targets = {}          # index -> (kind, title, space element|None)
        self._rects = {}            # index -> button rect, top-left coords
        self._geom_signature = None  # last-logged layout, to throttle dumps
        self._prev_layout = None      # last frame's positions, for motion detect
        self._screen_width = None
        self._active = False
        self._timer = None
        self._sync_timer = None
        self._hover_timer = None
        self._screen_height = None
        self._tap = None
        self._tap_source = None
        self._swallow_mouse_up = False
        # {app name: (window element, close button element)} accumulated
        # across space visits; see module docstring, quirk 2.
        self._fullscreen_registry = {}
        # {pid: [(ax_title, close button element), ...]} for the current
        # space, rebuilt each scan — used when an app hides its AX windows
        # during Mission Control (Notes, Steam, TextEdit, ...).
        self._window_registry = {}
        self._running_names = set()
        self._last_registry_scan = 0.0
        self._pending_close_title = None
        self._pending_close_attempts = 0
        self._pending_close_window = None
        self._pending_close_exit_title = None
        # Remaining split members to close after the current pending close
        # (the ✕2 button closes every app of a Split View tile in sequence).
        self._after_close_queue = []
        # Split halves no-op the AX close press, so ✕2 chains skip straight
        # to the traffic-light click; solo fullscreen tiles keep press-first.
        self._chain_click_first = False
        # {app name: (window el, close el, ax title)} — fullscreen closes
        # deferred until Mission Control exits (closing in place ghosts the
        # tile); applied/verified/escalated by processDeferredCloses.
        self._deferred_close = {}
        self._deferred_verify_title = None
        self._deferred_exit_title = None
        # (kind, title) -> deadline; keeps a just-closed target's ✕ from
        # flashing back during Mission Control's re-layout animation.
        self._recently_closed = {}
        # [(action, title)] window closes/minimizes queued to run once Mission
        # Control is dismissed (closing in place leaves a ghost thumbnail).
        self._deferred_actions = []
        # Synthetic traffic-light clicks for AX-opaque windows (Steam etc.)
        # whose deferred AX close/minimize no-oped — [(action, title, pid, x, y)].
        self._tl_queue = []
        self._tl_pending = None
        # App names whose (just un-fullscreened) main window is closed on MC
        # exit, by name with retries while the window un-parks.
        self._deferred_app_closes = []
        self._app_close_attempts = 0
        # To scrim the desktop thumbnail a closed full-screen app reappears as
        # (its title is unpredictable): the thumbnail titles present when the ✕
        # was clicked, so a title that appears afterwards can be marked.
        self._prev_thumb_titles = set()
        self._pending_unfs_baseline = None
        self._pending_unfs_expires = 0.0
        # Make-fullscreen (⤢) after MC exits: AX full-screen button, then a
        # green-button click fallback (the ⌃⌘F shortcut is ignored by
        # Electron/CEF apps). Staged across steps via these.
        self._fs_make_title = None
        self._fs_make_rect = None
        self._fs_make_owner = None
        # {title: glyph} thumbnails marked for a deferred action — shown with a
        # dim overlay so it is clear which windows will close on exit.
        self._marked = {}
        self._mark_panels = []       # reusable dim-overlay pool
        return self

    def start(self):
        if _DEBUG:
            try:
                with open(_GEOMETRY_LOG, "w") as fh:
                    fh.write("ExtraMissionControls geometry log\n")
            except OSError:
                pass
        self._start_detect_timer()
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            self, "spaceChanged:", NSWorkspaceActiveSpaceDidChangeNotification, None)
        # Window sets only change around app activations and space switches,
        # so the fullscreen/window registry is refreshed on those events
        # instead of on a constant polling cadence.
        center.addObserver_selector_name_object_(
            self, "appActivated:",
            "NSWorkspaceDidActivateApplicationNotification", None)
        # Stop the detect poll entirely while the display sleeps — Mission
        # Control can't be opened then, so there is nothing to look for.
        center.addObserver_selector_name_object_(
            self, "screensSlept:", "NSWorkspaceScreensDidSleepNotification", None)
        center.addObserver_selector_name_object_(
            self, "screensWoke:", "NSWorkspaceScreensDidWakeNotification", None)

    @objc.python_method
    def _start_detect_timer(self):
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                _DETECT_INTERVAL, self, "tick:", None, True)
            # Let macOS coalesce the idle wake-ups with other system work.
            self._timer.setTolerance_(0.15)

    @objc.python_method
    def _stop_detect_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def screensSlept_(self, note):
        if self._active:
            self._deactivate()
        self._stop_detect_timer()

    def screensWoke_(self, note):
        self._start_detect_timer()

    def stop(self):
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        self._stop_detect_timer()
        if self._active:
            self._deactivate()

    # -- Mission Control detection -------------------------------------------

    def tick_(self, timer):
        try:
            self._do_tick()
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _do_tick(self):
        if not self._active:
            # Idle path: a single WindowServer query; only touch the Dock's
            # AX tree when Mission Control actually looks active.
            if ax.mission_control_probably_active():
                group = ax.mission_control_group()
                if group is not None:
                    self._activate(group)
                    return
            if self._last_registry_scan == 0.0:
                self._scan_registry()  # first scan / forced post-MC rescan
            return
        if ax.mission_control_group() is None:
            self._deactivate()

    @objc.python_method
    def _activate(self, group):
        self._active = True
        # AX coordinates are relative to the primary screen's top-left.
        self._screen_height = NSScreen.screens()[0].frame().size.height
        self._screen_width = NSScreen.screens()[0].frame().size.width
        self._set_tap_enabled(True)
        self._start_hover_polling()
        self._sync_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            _SYNC_INTERVAL, self, "syncTick:", None, True)
        self._sync(group)

    @objc.python_method
    def _deactivate(self):
        self._active = False
        if self._sync_timer is not None:
            self._sync_timer.invalidate()
            self._sync_timer = None
        self._set_tap_enabled(False)
        self._stop_hover_polling()
        self._hide_all()
        self._prev_layout = None  # next open re-detects "still" from scratch
        self._last_registry_scan = 0.0  # spaces likely changed; rescan soon
        self._recently_closed = {}  # session-scoped suppression ends with MC
        self._marked = {}           # dim overlays clear with MC
        self._pending_unfs_baseline = None
        if self._deferred_actions:
            # Window closes/minimizes clicked during Mission Control land now,
            # with Mission Control gone, so they leave no ghost thumbnail.
            self.performSelector_withObject_afterDelay_(
                "applyDeferredActions", None, 0.35)
        if self._deferred_close:
            # Apps expose (and un-park) their AX windows shortly after Mission
            # Control closes; apply any deferred fullscreen closes then.
            self.performSelector_withObject_afterDelay_(
                "processDeferredCloses", None, 0.9)
        if self._deferred_app_closes:
            # Un-fullscreened apps: close their main window by name once MC is
            # gone and the window has un-parked (applyDeferredAppCloses retries).
            self._app_close_attempts = 0
            self.performSelector_withObject_afterDelay_(
                "applyDeferredAppCloses", None, 0.5)

    def syncTick_(self, timer):
        try:
            if not self._active:
                return
            group = ax.mission_control_group()
            if group is None:
                self._deactivate()
                return
            self._sync(group)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _sync(self, group):
        targets, marks = self._collect_targets(group)
        self._sync_panels(targets)
        self._sync_marks(marks)

    # -- fullscreen-window registry --------------------------------------------

    def spaceChanged_(self, notification):
        # Wait out the space-switch animation, then capture whatever
        # fullscreen windows just became visible to AX.
        self.performSelector_withObject_afterDelay_("rescanRegistry", None, 0.6)

    def appActivated_(self, notification):
        # New/changed windows almost always coincide with an app activation;
        # refresh the held references then (coalesced by _REGISTRY_MIN_GAP).
        self.performSelector_withObject_afterDelay_("rescanRegistry", None, 0.8)

    def rescanRegistry(self):
        if not self._active:
            self._scan_registry()

    @objc.python_method
    def _scan_registry(self):
        if os.environ.get("EMC_NO_REGISTRY"):
            return  # test hook: force the no-held-reference fallback paths
        now = time.time()
        if self._last_registry_scan and \
                now - self._last_registry_scan < _REGISTRY_MIN_GAP:
            return  # coalesce bursts of activation/space events
        self._last_registry_scan = now
        fresh, visible, by_pid = ax.scan_closable_windows()
        running = ax.running_app_names()
        merged = {}
        for name, entry in self._fullscreen_registry.items():
            if name not in running:
                continue  # app quit
            if name in visible and name not in fresh:
                continue  # app is visible and no longer fullscreen
            merged[name] = entry
        merged.update(fresh)
        self._fullscreen_registry = merged
        self._window_registry = by_pid
        self._running_names = running
        _debug("registry: fullscreen=%s windows=%d apps"
               % (sorted(merged), len(by_pid)))

    # -- targets ---------------------------------------------------------------

    @objc.python_method
    def _collect_targets(self, group):
        now = time.time()
        self._recently_closed = {key: deadline
                                 for key, deadline in self._recently_closed.items()
                                 if deadline > now}
        thumbs = ax.mission_control_thumbnails(group)
        spaces = ax.mission_control_spaces(group)
        self._log_geometry(group, thumbs, spaces)

        # A full-screen app closed from the Spaces Bar un-fullscreens in place
        # and reappears as a desktop thumbnail with an unpredictable title. Mark
        # whichever thumbnail title shows up that wasn't present when the ✕ was
        # clicked, so it gets the centred ✕ scrim like a desktop close.
        current_titles = {t["title"] for t in thumbs if t["title"]}
        if self._pending_unfs_baseline is not None:
            if now < self._pending_unfs_expires:
                for title in current_titles - self._pending_unfs_baseline:
                    if title not in self._marked:
                        self._marked[title] = "✕"
                        _geom_log("UNFS-SCRIM: %r reappeared -> marked" % title)
            else:
                self._pending_unfs_baseline = None
        self._prev_thumb_titles = current_titles

        # Track the Spaces Bar and the window thumbnails for motion SEPARATELY.
        # They animate independently: opening Mission Control and panning move
        # the window thumbnails, while hovering the top slides the bar in/out
        # without touching the windows. A button is only placed once its own
        # layer holds still (two identical consecutive frames), so window ✕
        # stay put while the bar slides and vice-versa — never chasing a moving
        # element to a wrong, unclickable spot.
        spaces_sig, thumbs_sig = self._layout_positions(spaces, thumbs)
        prev = self._prev_layout
        self._prev_layout = (spaces_sig, thumbs_sig)
        if prev is None:
            return [], []  # first frame after (re)activation: baseline
        spaces_still = spaces_sig == prev[0]
        thumbs_still = thumbs_sig == prev[1]

        # Distinguish an empty desktop overview from a fullscreen/split-space
        # view — the two states are otherwise identical in the AX tree, but in
        # the fullscreen view the whole subtree (bar and preview) reports stale
        # desktop-overview coordinates, so there is no reliable place for a ✕.
        # The tell: mc.windows is empty on a desktop, but has a (frame-less,
        # unreadable) child for the fullscreen space's window. Suppress there;
        # fullscreen apps are closed from a desktop overview's bar tiles.
        on_screen = [t for t in thumbs if self._on_screen(t)]
        if not on_screen and ax.mission_control_window_count(group) > 0:
            return [], []

        # Window-thumbnail buttons do not depend on the Spaces Bar: they show
        # as soon as the window layer is still, even while the bar is collapsed.
        # Ordered along the top-left like the macOS traffic lights:
        # ✕ close (red) · − minimize (yellow) · ⤢ full screen (green).
        # A window already marked (deferred close/minimize) shows a dim overlay
        # instead of buttons — a clear "this will close when you leave".
        targets, marks = [], []
        if thumbs_still:
            for thumb in on_screen:
                glyph = self._mark_glyph_for(thumb["title"])
                if glyph is not None:
                    marks.append((thumb, glyph))
                    continue
                targets.append(("window", self._slot(thumb, 0)))
                targets.append(("minimize",
                                self._slot(thumb, 1, "−", ui.HOVER_YELLOW)))
                targets.append(("makefullscreen",
                                self._slot(thumb, 2, "⤢", ui.HOVER_GREEN)))
                targets.append(("quit",
                                self._slot(thumb, 3, "⏻", ui.HOVER_PURPLE)))

        # Bar-tile buttons only exist while the bar is still AND expanded at the
        # top of the screen; collapsed tiles sit above the screen edge (negative
        # y) so there is nowhere to draw them until the user hovers the bar open.
        # Ordered top-left: ✕ close (red) · ❏ exit full screen (green). A
        # fullscreen space cannot be minimized, so no minimize button here.
        if not (spaces_still and self._bar_settled(spaces)):
            return targets, marks
        for space in spaces:
            if not self._on_screen(space):
                continue
            if ("acted", space["title"]) in self._recently_closed:
                continue
            glyph = self._mark_glyph_for(space["title"])
            if glyph is not None:  # queued for a deferred close → dim scrim
                marks.append((space, glyph))
                continue
            apps = self._split_apps(space)
            if apps:
                # Split View: closing one half reliably is not possible (most
                # apps no-op their close button while tiled and macOS tears
                # the pair apart regardless), so a single button closes BOTH,
                # labeled ✕2 to say so.
                targets.append(("splitall",
                                self._slot(space, 0, "✕%d" % len(apps))))
            elif self._is_fullscreen_tile(space):
                targets.append(("fullscreen", self._slot(space, 0)))
            else:
                continue
            targets.append(("unfullscreen",
                            self._slot(space, 1, "❏", ui.HOVER_GREEN)))
        return targets, marks

    @objc.python_method
    def _mark_glyph_for(self, title):
        """The dim-scrim glyph for a tile/thumbnail title, or None. Exact match
        first, else a UNIQUE prefix relation — a full-screen app un-fullscreened
        by the ✕ reappears as a desktop thumbnail whose title the Dock may
        shorten relative to the window's AX title we marked it under."""
        glyph = self._marked.get(title)
        if glyph is not None:
            return glyph
        related = [g for t, g in self._marked.items()
                   if ax.titles_related(title, t)]
        return related[0] if len(related) == 1 else None

    @objc.python_method
    def _slot(self, tile, slot, label=None, hover=None):
        """A per-button copy of a tile/thumbnail dict placed in the given
        top-left slot (0,1,2,...), with an optional glyph and hover colour."""
        out = dict(tile)
        out["slot"] = slot
        if label is not None:
            out["label"] = label
        if hover is not None:
            out["hover"] = hover
        return out

    @objc.python_method
    def _layout_positions(self, spaces, thumbs):
        """A rounded snapshot of every tile/thumbnail rect, compared frame to
        frame to tell whether Mission Control is animating or holding still."""
        return (
            tuple((round(s["x"]), round(s["y"]),
                   round(s["width"]), round(s["height"])) for s in spaces),
            tuple((round(t["x"]), round(t["y"]),
                   round(t["width"]), round(t["height"])) for t in thumbs))

    @objc.python_method
    def _bar_settled(self, spaces):
        """True only when the Spaces Bar is expanded at the top of the screen
        (full-height tiles at non-negative y) — the one state in which our
        button coordinates line up with what's on screen."""
        if not spaces:
            return False
        return (min(s["height"] for s in spaces) >= _MIN_BAR_TILE_HEIGHT
                and min(s["y"] for s in spaces) >= 0)

    @objc.python_method
    def _on_screen(self, tile):
        """True when the tile/thumbnail's center is within the display. In
        Mission Control the window thumbnails slide with the pan, so one whose
        center has left the screen belongs to a space you've panned away from
        and gets no button. The Spaces Bar tiles stay put at the top, so their
        buttons remain even while you view a fullscreen space."""
        w = self._screen_width or 0.0
        h = self._screen_height or 0.0
        cx = tile["x"] + tile["width"] / 2.0
        cy = tile["y"] + tile["height"] / 2.0
        return 0 <= cx <= w and 0 <= cy <= h

    @objc.python_method
    def _log_geometry(self, group, thumbs, spaces):
        """Append raw AX geometry (once per distinct layout) to
        _GEOMETRY_LOG for diagnosis. Debug builds only (EMC_DEBUG=1) — in
        normal use this method is free."""
        if not _DEBUG:
            return
        signature = (
            tuple((s["title"], round(s["x"]), round(s["y"]),
                   round(s["width"]), round(s["height"])) for s in spaces),
            tuple((t["title"], round(t["x"]), round(t["y"]),
                   round(t["width"]), round(t["height"])) for t in thumbs))
        if signature == self._geom_signature:
            return
        self._geom_signature = signature
        lines = ["---- layout (screen %.0fx%.0f) ----"
                 % (self._screen_width or 0, self._screen_height or 0)]
        for s in spaces:
            element = s.get("element")
            sel = ax._attribute(element, "AXSelected") if element else None
            val = ax._attribute(element, "AXValue") if element else None
            lines.append("  space  %-22r @(%.0f,%.0f  %.0fx%.0f)  sel=%s val=%s"
                         % (s["title"], s["x"], s["y"], s["width"],
                            s["height"], sel, val))
        for t in thumbs:
            lines.append("  window %-22r @(%.0f,%.0f  %.0fx%.0f)"
                         % (t["title"], t["x"], t["y"], t["width"], t["height"]))
        # No window thumbnails is the ambiguous case (empty desktop overview vs
        # fullscreen-space view): dump the full subtree to find what differs.
        if not thumbs:
            lines.append("  [mc tree]")
            lines.extend(ax.describe_mc_tree(group))
        text = "\n".join(lines)
        if _DEBUG:
            print("EMC:", text, flush=True)
        try:
            with open(_GEOMETRY_LOG, "a") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    @objc.python_method
    def _split_apps(self, space):
        """If this Spaces Bar tile is a Split View (two apps sharing one
        fullscreen space), return the app names in left-to-right order;
        else None. A Split View tile reads title='A & B',
        desc='exit to full screen A & B'."""
        title = space["title"]
        desc = space.get("desc") or ""
        if " & " not in title or "full screen" not in desc:
            return None
        parts = [p.strip() for p in title.split(" & ")]
        # Guard against an app literally named "Foo & Bar": every part must
        # be a real running app (or one we hold a fullscreen reference for).
        if all(p and (p in self._running_names or p in self._fullscreen_registry)
               for p in parts):
            return parts
        return None

    @objc.python_method
    def _is_fullscreen_tile(self, space):
        """True for Spaces Bar tiles backed by a fullscreen app. Desktop
        tiles fail all three tests (no registry entry, desc like 'exit to
        Desktop 1', no running app named 'Desktop 1')."""
        title = space["title"]
        if not title:
            return False
        if title in self._fullscreen_registry:
            return True
        if "full screen" in (space.get("desc") or ""):
            return True
        return title in self._running_names

    # -- click interception ------------------------------------------------

    @objc.python_method
    def _set_tap_enabled(self, enabled):
        if self._tap is None:
            if not enabled:
                return
            mask = (Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseUp))
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                mask, self._tap_callback, None)
            if self._tap is None:
                print("ExtraMissionControls: could not create event tap — "
                      "✕ buttons in Mission Control will not receive clicks "
                      "(is Accessibility permission granted?)")
                return
            self._tap_source = Quartz.CFMachPortCreateRunLoopSource(
                None, self._tap, 0)
            Quartz.CFRunLoopAddSource(
                Quartz.CFRunLoopGetMain(), self._tap_source,
                Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, enabled)
        if not enabled:
            self._swallow_mouse_up = False

    @objc.python_method
    def _tap_callback(self, proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            if self._active:
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        location = Quartz.CGEventGetLocation(event)
        if event_type == Quartz.kCGEventLeftMouseDown:
            index = self._button_index_at(location.x, location.y)
            if index is not None:
                self._swallow_mouse_up = True
                AppHelper.callAfter(self._close_index, index)
                return None  # consumed: don't let Mission Control see it
        elif event_type == Quartz.kCGEventLeftMouseUp and self._swallow_mouse_up:
            self._swallow_mouse_up = False
            return None
        return event

    @objc.python_method
    def _button_index_at(self, x, y):
        for index, (bx, by, bw, bh) in self._rects.items():
            if bx <= x <= bx + bw and by <= y <= by + bh:
                return index
        return None

    # -- hover ---------------------------------------------------------------
    # Tracking areas never fire while Mission Control owns the mouse, so we
    # poll the cursor position instead and set hover state explicitly.

    @objc.python_method
    def _start_hover_polling(self):
        if self._hover_timer is None:
            self._hover_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                _HOVER_INTERVAL, self, "hoverTick:", None, True)

    @objc.python_method
    def _stop_hover_polling(self):
        if self._hover_timer is not None:
            self._hover_timer.invalidate()
            self._hover_timer = None
        for button in self._buttons:
            button.setHovered_(False)

    def hoverTick_(self, timer):
        location = NSEvent.mouseLocation()  # global, bottom-left origin
        x = location.x
        y_top = self._screen_height - location.y
        hit = self._button_index_at(x, y_top)
        for index, button in enumerate(self._buttons):
            button.setHovered_(index == hit)

    # -- panel management ------------------------------------------------------

    @objc.python_method
    def _sync_panels(self, targets):
        for index, (kind, tile) in enumerate(targets):
            panel = self._panel_at(index)
            button = self._buttons[index]
            # Scale the ✕ down on small tiles (collapsed Spaces Bar).
            size = min(ui.CLOSE_BUTTON_SIZE,
                       max(_MIN_BUTTON_SIZE, tile["height"] * 0.45))
            self._targets[index] = (kind, tile["title"], tile.get("element"))
            # Lay the buttons out left-to-right from the tile's top-left corner.
            x = tile["x"] + _BUTTON_INSET + tile.get("slot", 0) * (size + _BUTTON_GAP)
            y_top = tile["y"] + _BUTTON_INSET
            self._rects[index] = (x, y_top, size, size)
            y = self._screen_height - y_top - size  # flip to bottom-left origin
            panel.setFrame_display_(NSMakeRect(x, y, size, size), True)
            button.setLabel_(tile.get("label", "✕"))
            button.setHoverRGB_(tile.get("hover", ui.HOVER_RED))
            if abs(button.frame().size.width - size) > 0.5:
                button.setFrame_(NSMakeRect(0, 0, size, size))
                button.layer().setCornerRadius_(size / 2)
            if not panel.isVisible():
                panel.orderFrontRegardless()
                _debug("panel %d (%s %r) shown at (%.0f,%.0f) size=%.0f"
                       % (index, kind, tile["title"], x, y_top, size))
        for index in range(len(targets), len(self._panels)):
            self._panels[index].orderOut_(None)
            self._targets.pop(index, None)
            self._rects.pop(index, None)

    @objc.python_method
    def _panel_at(self, index):
        while len(self._panels) <= index:
            self._panels.append(self._make_panel(len(self._panels)))
        return self._panels[index]

    @objc.python_method
    def _make_panel(self, index):
        size = ui.CLOSE_BUTTON_SIZE
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, size, size),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setHidesOnDeactivate_(False)
        # Above the Mission Control backdrop (Dock windows sit at layer ~20),
        # present on every space, and pinned in place while MC animates.
        panel.setLevel_(NSPopUpMenuWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle)

        button = ui.make_close_button(self, "closeClicked:")
        button.setTag_(index)
        panel.contentView().addSubview_(button)
        self._buttons.append(button)
        return panel

    # -- "marked for close" dim overlays --------------------------------------

    @objc.python_method
    def _sync_marks(self, marks):
        """Draw a dim scrim + glyph over each thumbnail queued for a deferred
        action; hide any leftover overlay panels from a previous frame."""
        for index, (tile, glyph) in enumerate(marks):
            panel = self._mark_panel_at(index)
            w, h = tile["width"], tile["height"]
            y = self._screen_height - tile["y"] - h  # flip to bottom-left origin
            panel.setFrame_display_(NSMakeRect(tile["x"], y, w, h), True)
            view = panel.contentView()
            if (abs(view.frame().size.width - w) > 0.5
                    or abs(view.frame().size.height - h) > 0.5):
                view.setFrame_(NSMakeRect(0, 0, w, h))
                view.setNeedsDisplay_(True)
            view.setGlyph_(glyph)
            if not panel.isVisible():
                panel.orderFrontRegardless()
        for index in range(len(marks), len(self._mark_panels)):
            self._mark_panels[index].orderOut_(None)

    @objc.python_method
    def _mark_panel_at(self, index):
        while len(self._mark_panels) <= index:
            self._mark_panels.append(self._make_mark_panel())
        return self._mark_panels[index]

    @objc.python_method
    def _make_mark_panel(self):
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 10, 10),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(True)  # decorative; let pans/clicks pass
        # Just under the button panels so a ✕/− button is never covered by a
        # scrim, but above the Mission Control backdrop.
        panel.setLevel_(NSPopUpMenuWindowLevel - 1)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setContentView_(ui.make_mark_overlay(NSMakeRect(0, 0, 10, 10)))
        return panel

    def _hide_all(self):
        for panel in self._panels:
            panel.orderOut_(None)
        for panel in self._mark_panels:
            panel.orderOut_(None)
        self._targets = {}
        self._rects = {}

    # -- actions ---------------------------------------------------------------

    def closeClicked_(self, sender):
        # Direct button action — only reachable if window hit-testing works
        # (it normally doesn't during Mission Control; the tap handles it).
        self._close_index(sender.tag())

    @objc.python_method
    def _close_index(self, index):
        kind, title, element = self._targets.get(index, (None, None, None))
        if not title:
            return
        if kind == "fullscreen":
            self._close_fullscreen(index, title, element)
            return
        if kind == "splitall":
            self._close_split_all(index, title, element)
            return
        if kind == "unfullscreen":
            self._unfullscreen_tile(index, title, element)
            return
        if kind == "makefullscreen":
            self._make_fullscreen(index, title, element)
            return
        if kind == "minimize":
            self._defer_window_action("minimize", title)
            return
        if kind == "quit":
            # Quit the whole app (⌘Q), not just this window — deferred like a
            # close so the app (and all its thumbnails) leave no ghost.
            self._defer_window_action("quit", title)
            return
        # kind == "window": close it. Closing (or minimizing) a windowed
        # thumbnail *while Mission Control is open* leaves a ghost thumbnail the
        # Dock never removes — clicking it reopens the app (verified). So defer
        # the action until Mission Control is dismissed, when it lands like an
        # ordinary close with no ghost.
        self._defer_window_action("close", title)

    @objc.python_method
    def _defer_window_action(self, action, title):
        """Queue a window close/minimize/quit to run when Mission Control exits.
        The buttons vanish now and the thumbnail gets a dim scrim (✕ close, −
        minimize, ⏻ quit) for the rest of the session, so it's clear what will
        act on exit and the buttons can never reappear on a lingering thumbnail."""
        if not any(t == title for _, t in self._deferred_actions):
            self._deferred_actions.append((action, title))
        # Show a dim "marked" overlay on this thumbnail instead of its buttons.
        self._marked[title] = {"minimize": "−", "quit": "⏻"}.get(action, "✕")
        for i, (k, t, e) in list(self._targets.items()):
            if t == title:
                self._panels[i].orderOut_(None)
                self._rects.pop(i, None)
        _geom_log("DEFER %s window=%r (applies on MC exit)" % (action, title))

    def applyDeferredActions(self):
        if not self._deferred_actions:
            return
        if ax.mission_control_group() is not None:
            # Mission Control re-opened before we could apply — wait again so
            # we never act in place (which would leave a ghost).
            self.performSelector_withObject_afterDelay_(
                "applyDeferredActions", None, 0.4)
            return
        actions, self._deferred_actions = self._deferred_actions, []
        infos = windows.list_windows(exclude_pid=os.getpid())
        pids = list({i.pid for i in infos})
        for action, title in actions:
            if action == "minimize":
                ok = (ax.minimize_window_by_title(title, pids)
                      or self._press_held_minimize(title, infos))
            elif action == "quit":
                # Quit the whole app (⌘Q), by pid — no title/AX-button matching.
                ok = self._quit_app_for(title, infos)
            else:
                ok = (ax.close_window_by_title(title, pids)
                      or self._press_held_window(title, infos))
            if not ok and action in ("close", "minimize"):
                # AX-opaque window (Steam and other CEF/game windows expose no
                # close/minimize button): fall back to a synthetic click on the
                # native traffic light, queued so we can raise then click.
                info = self._match_window_info(title, infos)
                if info is not None:
                    self._tl_queue.append(
                        (action, title, info.pid, info.x, info.y))
            _geom_log("APPLY-DEFERRED %s %r -> %s" % (action, title, ok))
        if self._tl_queue:
            self.performSelector_withObject_afterDelay_(
                "processTrafficLightQueue", None, 0.05)

    @objc.python_method
    def _quit_app_for(self, title, infos):
        """Quit the app owning the thumbnail titled `title` (⌘Q, graceful — it
        may prompt to save). By pid, so it works regardless of AX buttons."""
        info = self._match_window_info(title, infos)
        if info is None:
            return False
        return ax.quit_pid(info.pid)

    @objc.python_method
    def _match_window_info(self, title, infos):
        """The on-screen CG window best matching a thumbnail title — exact
        title or owner, else a unique prefix relation (the Dock shortens the
        titles it shows, and a window may be owned by a helper process, e.g.
        Steam's visible window belongs to 'Steam Helper')."""
        exact = [i for i in infos if title in (i.title, i.owner)]
        if exact:
            return exact[0]
        related = [i for i in infos
                   if ax.titles_related(title, i.title)
                   or ax.titles_related(title, i.owner)]
        return related[0] if len(related) == 1 else None

    def processTrafficLightQueue(self):
        if not self._tl_queue:
            return
        action, title, pid, x, y = self._tl_queue[0]
        # These windows ignore clicks unless active — raise the app first, then
        # click after a beat so the raise has landed.
        ax.activate_pid(pid)
        self._tl_pending = (action, title, x, y)
        self.performSelector_withObject_afterDelay_(
            "clickTrafficLight", None, 0.3)

    def clickTrafficLight(self):
        pending, self._tl_pending = self._tl_pending, None
        if self._tl_queue:
            self._tl_queue.pop(0)
        if pending is not None:
            action, title, x, y = pending
            # Traffic lights sit at the window's top-left corner: close (red)
            # ~x+20, minimize (yellow) ~x+40, both ~19px below the top edge.
            cx = x + (40 if action == "minimize" else 20)
            cy = y + 19
            self._post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
            self._post_mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
            self._post_mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
            _geom_log("TRAFFIC-LIGHT %s %r at (%.0f,%.0f)"
                      % (action, title, cx, cy))
        if self._tl_queue:
            self.performSelector_withObject_afterDelay_(
                "processTrafficLightQueue", None, 0.4)

    def applyDeferredAppCloses(self):
        """Close a just-un-fullscreened app's main window once Mission Control
        is gone. By name (not title — the AX title changes across the
        fullscreen→desktop move), retrying while the window finishes un-parking,
        then a close traffic-light click for AX-opaque apps (Steam)."""
        if not self._deferred_app_closes:
            return
        if ax.mission_control_group() is not None:
            self.performSelector_withObject_afterDelay_(
                "applyDeferredAppCloses", None, 0.4)
            return
        app = self._deferred_app_closes[0]
        if ax.close_main_window(app):
            _geom_log("APPLY-APP-CLOSE %r -> closed (attempt %d)"
                      % (app, self._app_close_attempts))
            self._finish_app_close(app)
            return
        self._app_close_attempts += 1
        if self._app_close_attempts < 6:  # window may still be un-parking
            self.performSelector_withObject_afterDelay_(
                "applyDeferredAppCloses", None, 0.4)
            return
        _geom_log("APPLY-APP-CLOSE %r -> AX close failed; traffic-light" % app)
        self._click_app_close(app)
        self._finish_app_close(app)

    @objc.python_method
    def _finish_app_close(self, app):
        if app in self._deferred_app_closes:
            self._deferred_app_closes.remove(app)
        self._app_close_attempts = 0
        if self._deferred_app_closes:
            self.performSelector_withObject_afterDelay_(
                "applyDeferredAppCloses", None, 0.3)

    @objc.python_method
    def _click_app_close(self, app_name):
        info = self._match_window_info(
            app_name, windows.list_windows(exclude_pid=os.getpid()))
        if info is None:
            return False
        self._tl_queue.append(("close", app_name, info.pid, info.x, info.y))
        self.performSelector_withObject_afterDelay_(
            "processTrafficLightQueue", None, 0.05)
        return True

    @objc.python_method
    def _press_held_window(self, title, infos):
        """Close via references captured before Mission Control opened.
        Stale references (window already gone) fail the press harmlessly."""
        return self._press_held_button(title, infos, 1)

    @objc.python_method
    def _press_held_minimize(self, title, infos):
        """Minimize via the held minimize button (index 2). None for windows
        that expose no minimize button."""
        return self._press_held_button(title, infos, 2)

    @objc.python_method
    def _press_held_button(self, title, infos, slot):
        """Press a held window button captured before Mission Control opened —
        slot 1 = close, slot 2 = minimize. Match the thumbnail title to a held
        window: exact title, else a UNIQUE prefix match (the Dock shortens the
        title it shows), else the pid single-window fallback."""
        entries = [e for es in self._window_registry.values() for e in es]
        for entry in entries:  # exact title
            if entry[0] == title and entry[slot] is not None \
                    and ax.press_element(entry[slot]):
                return True
        related = [e for e in entries
                   if e[slot] is not None and ax.titles_related(title, e[0])]
        if len(related) == 1 and ax.press_element(related[0][slot]):
            return True
        for info in infos:  # a pid that owns exactly one held window
            if info.title == title:
                es = self._window_registry.get(info.pid) or []
                if len(es) == 1 and es[0][slot] is not None \
                        and ax.press_element(es[0][slot]):
                    return True
        return False

    @objc.python_method
    def _make_fullscreen(self, index, title, element):
        """Send a window thumbnail's window to full screen. A thumbnail has no
        AX 'enter full screen' action, and while Mission Control is open apps
        park their windows so setting AXFullScreen on a held reference is a
        silent no-op (verified). So we AXPress the thumbnail — which focuses
        that window and leaves Mission Control (you end up looking at it) — then
        CLICK its green full-screen traffic light. The old ⌃⌘F shortcut was
        ignored by Electron/CEF apps (Claude, Steam) and needed exact focus;
        the button click works on any app with standard window controls."""
        if element is None:
            return
        self._panels[index].orderOut_(None)
        self._rects.pop(index, None)
        _geom_log("MAKE-FULLSCREEN window=%r" % title)
        ax.press_element(element)  # focus the window, exit Mission Control
        self._fs_make_title = title
        self.performSelector_withObject_afterDelay_(
            "makeFullscreenStep", None, 0.6)

    def makeFullscreenStep(self):
        title = self._fs_make_title
        self._fs_make_title = None
        if not title:
            return
        # Mission Control has exited and the window is un-parked. Find it and
        # raise its app frontmost.
        info = self._match_window_info(
            title, windows.list_windows(exclude_pid=os.getpid()))
        if info is None:
            _geom_log("MAKE-FULLSCREEN %r: no CG window match" % title)
            return
        ax.activate_pid(info.pid)
        self._fs_make_rect = (title, info.x, info.y)
        self._fs_make_owner = info.owner
        # Prefer the AX full-screen button — no pixel coordinates, so it works
        # for apps like Claude whose traffic lights are inset off the standard
        # position. Verify shortly; if it didn't take (or the window is AX-opaque
        # like Steam), fall back to a synthetic click on the green button.
        if ax.fullscreen_window_of(info.owner):
            self.performSelector_withObject_afterDelay_(
                "verifyMakeFullscreen", None, 1.0)
        else:
            self.clickFullscreenButton()

    def verifyMakeFullscreen(self):
        data = self._fs_make_rect
        if not data:
            return
        title = data[0]
        if (ax.find_fullscreen_window(self._fs_make_owner) is not None
                or ax.find_fullscreen_window_by_title(title) is not None):
            self._fs_make_rect = None
            _geom_log("MAKE-FULLSCREEN %r via AX button" % title)
            return
        _geom_log("MAKE-FULLSCREEN %r AX no-op; clicking green button" % title)
        self.clickFullscreenButton()

    def clickFullscreenButton(self):
        data, self._fs_make_rect = self._fs_make_rect, None
        if not data:
            return
        title, x, y = data
        # The green full-screen button is the third traffic light: ~x+60, and
        # ~19px below the window's top edge (close is x+20, minimize x+40).
        cx, cy = x + 60, y + 19
        self._post_mouse(Quartz.kCGEventMouseMoved, cx, cy)
        self._post_mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
        self._post_mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
        _geom_log("MAKE-FULLSCREEN click %r at (%.0f,%.0f)" % (title, cx, cy))

    @objc.python_method
    def _unfullscreen_tile(self, index, title, element):
        """Exit full screen for a Spaces Bar tile without closing the app —
        the Dock's own AXRemoveDesktop action, which turns the fullscreen (or
        split) space back into ordinary window(s) while Mission Control stays
        open. No space switch, no window close."""
        if element is None:
            return
        self._panels[index].orderOut_(None)
        self._rects.pop(index, None)
        if ax.remove_space(element):
            _geom_log("UNFULLSCREEN tile=%r" % title)
            self._recently_closed[("acted", title)] = time.time() + 2.5
        else:
            print("ExtraMissionControls: could not exit full screen for %r"
                  % title)

    @objc.python_method
    def _close_split_all(self, index, title, element):
        """Close BOTH apps of a Split View tile (the ✕2 button). Switches to
        the split space, closes the first app there; the surviving half turns
        into a solo fullscreen space we are already viewing, so the second app
        is closed right after, then Mission Control reopens."""
        if element is None:
            print("ExtraMissionControls: cannot reach split %r" % title)
            return
        self._panels[index].orderOut_(None)
        self._rects.pop(index, None)
        self._recently_closed[("acted", title)] = time.time() + 6.0
        apps = [p.strip() for p in title.split(" & ") if p.strip()]
        _geom_log("SPLIT-CLOSE-ALL tile=%r apps=%s" % (title, apps))
        self._after_close_queue = apps[1:]
        self._pending_close_title = apps[0]
        self._pending_close_attempts = 10
        self._chain_click_first = True
        ax.press_element(element)  # switch to the split space; MC exits
        self.performSelector_withObject_afterDelay_(
            "finishPendingClose", None, 0.6)

    @objc.python_method
    def _pending_chain_done(self):
        """One pending close finished; close the next split member if any
        (we are already viewing its now-solo space), else back into MC."""
        if self._after_close_queue:
            next_title = self._after_close_queue.pop(0)
            _geom_log("  chain: closing next split member %r" % next_title)
            self._pending_close_title = next_title
            self._pending_close_attempts = 8
            self.performSelector_withObject_afterDelay_(
                "finishPendingClose", None, 0.7)
        else:
            self._chain_click_first = False
            self._reopen_mission_control(0.6)

    @objc.python_method
    def _pending_chain_failed(self, title):
        if self._after_close_queue:
            _geom_log("  chain: %r failed; abandoning %s"
                      % (title, self._after_close_queue))
            self._after_close_queue = []
        self._chain_click_first = False
        self._reopen_mission_control(1.0)

    @objc.python_method
    def _close_fullscreen(self, index, title, element):
        """Close a full-screen app from the Spaces Bar the way the user prefers:
        first drop it back to the desktop — the Dock's own AXRemoveDesktop, the
        same action as the ❏ button, so it stays in Mission Control and the app
        reappears as an ordinary window thumbnail — then mark THAT thumbnail for
        a deferred close. It shows the centred ✕ scrim and closes on Mission
        Control exit, exactly like a window closed from the desktop (no ghost)."""
        entry = self._fullscreen_registry.get(title)
        self._panels[index].orderOut_(None)
        self._rects.pop(index, None)
        if element is not None and ax.remove_space(element):
            _geom_log("CLOSE-FULLSCREEN %r: un-fullscreened, deferring close"
                      % title)
            # Suppress the collapsing tile's own buttons during the transition.
            self._recently_closed[("acted", title)] = time.time() + 2.5
            self._fullscreen_registry.pop(title, None)
            # Scrim the desktop thumbnail it reappears as. Its title is
            # unpredictable, so _collect_targets marks whichever thumbnail
            # appears that isn't in this baseline (captured pre-transition).
            self._pending_unfs_baseline = set(self._prev_thumb_titles)
            self._pending_unfs_expires = time.time() + 4.0
            # Reliable close once MC exits: close the app's MAIN window BY NAME.
            # (Title matching is unreliable for a just-un-fullscreened window —
            # its AX title often differs from the full-screen-era title we held —
            # and close_main_window also retries while the window un-parks.)
            if title not in self._deferred_app_closes:
                self._deferred_app_closes.append(title)
            return
        # AXRemoveDesktop unavailable/failed. Fall back to the held-ref deferred
        # close, else the switch-to-space close (both leave no ghost).
        if entry is not None:
            _geom_log("CLOSE-FULLSCREEN %r: no un-fullscreen; held deferred close"
                      % title)
            self._deferred_close[title] = entry
            self._marked[title] = "✕"
            return
        self._fallback_close_fullscreen(title, element)

    @objc.python_method
    def _fallback_close_fullscreen(self, title, element):
        if element is None:
            print("ExtraMissionControls: no closable fullscreen named %r" % title)
            return
        # Universal fallback: switch to its space — Mission Control closes,
        # the app becomes AX-visible — then close it there (verified, with
        # un-fullscreen escalation), and re-enter Mission Control.
        _debug("fallback: switching to space %r to close it" % title)
        self._pending_close_title = title
        self._pending_close_attempts = 10
        self._chain_click_first = False
        ax.press_element(element)
        self.performSelector_withObject_afterDelay_(
            "finishPendingClose", None, 0.6)

    @objc.python_method
    def _reopen_mission_control(self, delay):
        self.performSelector_withObject_afterDelay_(
            "reopenMissionControl", None, delay)

    def reopenMissionControl(self):
        # Only open if Mission Control is currently closed — otherwise the
        # exposelauncher toggle would close an already-open Mission Control.
        if ax.mission_control_group() is None:
            subprocess.Popen(["open", "-b", "com.apple.exposelauncher"])

    def finishPendingClose(self):
        title = self._pending_close_title
        if title is None:
            return
        window = ax.find_fullscreen_target(title)
        if window is None:
            # The space switch may still be animating; wait for the app to
            # become AX-visible on its own space.
            self._pending_close_attempts -= 1
            if self._pending_close_attempts > 0:
                self.performSelector_withObject_afterDelay_(
                    "finishPendingClose", None, 0.35)
            else:
                self._pending_close_title = None
                print("ExtraMissionControls: gave up closing fullscreen %r"
                      % title)
                self._pending_chain_failed(title)
            return
        ax.focus_window(window)
        self._pending_close_window = window
        if self._chain_click_first:
            # Split members ignore AX close presses — go straight for the
            # real traffic light. Start the title-bar reveal (cursor to the
            # top edge) now, so it slides in while we wait.
            frame = ax.window_frame(window)
            if frame is not None:
                _geom_log("  finishPendingClose: %r — traffic-light first "
                          "(x=%.0f)" % (title, frame[0]))
                self._post_mouse(Quartz.kCGEventMouseMoved, frame[0] + 20, 5)
                self.performSelector_withObject_afterDelay_(
                    "clickPendingTrafficLight", None, 0.55)
                return
        _geom_log("  finishPendingClose: focusing %r, then pressing close" % title)
        self.performSelector_withObject_afterDelay_(
            "pressPendingClose", None, 0.25)

    def pressPendingClose(self):
        title = self._pending_close_title
        window = self._pending_close_window
        self._pending_close_window = None
        if title is None or window is None:
            return
        button = ax.close_button_of(window)
        if button is not None:
            ax.press_element(button)
        else:
            ax.close_fullscreen_window(title)
        self.performSelector_withObject_afterDelay_(
            "verifyPendingClose", None, 0.6)

    def verifyPendingClose(self):
        title = self._pending_close_title
        if title is None:
            return
        window = ax.find_fullscreen_target(title)
        if window is None:
            # Really closed: the space collapsed.
            _geom_log("  verifyPendingClose: %r closed by close-button press" % title)
            self._pending_close_title = None
            self._fullscreen_registry.pop(title, None)
            self._pending_chain_done()
            return
        # The AX close press no-oped (split halves ignore it even focused).
        # Click the REAL traffic light instead: cursor to the top edge reveals
        # the title bars in Split View / fullscreen, then click the red button
        # at this window's top-left corner — the native close, so the partner
        # half survives exactly as if the user clicked it themselves.
        frame = ax.window_frame(window)
        if frame is not None:
            _geom_log("  verifyPendingClose: %r still fullscreen; clicking its "
                      "traffic light (window at x=%.0f)" % (title, frame[0]))
            self._pending_close_window = window
            self._post_mouse(Quartz.kCGEventMouseMoved, frame[0] + 20, 5)
            self.performSelector_withObject_afterDelay_(
                "clickPendingTrafficLight", None, 0.55)
            return
        self._escalate_unfullscreen(title, window)

    def clickPendingTrafficLight(self):
        title = self._pending_close_title
        window = self._pending_close_window
        self._pending_close_window = None
        if title is None or window is None:
            return
        frame = ax.window_frame(window)
        x = (frame[0] if frame else 0.0) + 20.0
        # The revealed title-bar strip occupies the top ~33pt of the screen.
        self._post_mouse(Quartz.kCGEventMouseMoved, x, 16)
        self._post_mouse(Quartz.kCGEventLeftMouseDown, x, 16)
        self._post_mouse(Quartz.kCGEventLeftMouseUp, x, 16)
        self.performSelector_withObject_afterDelay_(
            "verifyTrafficLightClose", None, 0.7)

    def verifyTrafficLightClose(self):
        title = self._pending_close_title
        if title is None:
            return
        window = ax.find_fullscreen_target(title)
        if window is None:
            _geom_log("  verifyTrafficLightClose: %r closed by traffic light" % title)
            self._pending_close_title = None
            self._fullscreen_registry.pop(title, None)
            self._pending_chain_done()
            return
        _geom_log("  verifyTrafficLightClose: %r survived; un-fullscreening" % title)
        self._escalate_unfullscreen(title, window)

    @objc.python_method
    def _escalate_unfullscreen(self, title, window):
        # Last resort: un-fullscreen, then close the resulting normal window.
        self._pending_close_title = None
        _debug("un-fullscreening %r to close it" % title)
        if ax.set_fullscreen(window, False):
            self._pending_close_exit_title = title
            self.performSelector_withObject_afterDelay_(
                "finishExitedClose", None, 1.9)
        else:
            print("ExtraMissionControls: could not close fullscreen %r" % title)
            self._pending_chain_failed(title)

    @objc.python_method
    def _post_mouse(self, kind, x, y):
        event = Quartz.CGEventCreateMouseEvent(
            None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.06)

    def processDeferredCloses(self):
        # One title at a time so the verify/escalate steps below can use single
        # ivars; each finishes by chaining to the next via _next_deferred_close.
        if not self._deferred_close:
            return
        title, entry = next(iter(self._deferred_close.items()))
        self._deferred_close.pop(title, None)
        window, held_button, ax_title = entry
        # The window element can be recreated by the fullscreen transition;
        # re-find it now that the app's AX is visible again post-exit.
        if not ax.element_alive(window):
            window = ax.find_app_window(title, window, ax_title)
        if window is None:
            _debug("deferred close of %r: already gone" % title)
            self._next_deferred_close()
            return
        button = ax.close_button_of(window) or held_button
        if button is not None:
            ax.press_element(button)
        self._deferred_verify_title = title
        self.performSelector_withObject_afterDelay_(
            "verifyDeferredClose", None, 0.8)

    def verifyDeferredClose(self):
        title = self._deferred_verify_title
        self._deferred_verify_title = None
        if not title:
            return
        window = ax.find_fullscreen_target(title)
        if window is None:  # tile/window gone — the close landed
            _debug("deferred close of %r completed" % title)
            self._fullscreen_registry.pop(title, None)
            self._next_deferred_close()
            return
        # The close press was ignored while still fullscreen; un-fullscreen it
        # (works once Mission Control is gone), then close the normal window.
        if ax.set_fullscreen(window, False):
            self._deferred_exit_title = title
            self.performSelector_withObject_afterDelay_(
                "finishDeferredExitClose", None, 1.9)
        else:
            print("ExtraMissionControls: could not finish closing %r" % title)
            self._next_deferred_close()

    def finishDeferredExitClose(self):
        title = self._deferred_exit_title
        self._deferred_exit_title = None
        if not title:
            return
        if ax.close_main_window(title):
            _debug("deferred close of %r completed after un-fullscreen" % title)
            self._fullscreen_registry.pop(title, None)
        else:
            print("ExtraMissionControls: un-fullscreened %r but could not "
                  "close it" % title)
        self._next_deferred_close()

    @objc.python_method
    def _next_deferred_close(self):
        if self._deferred_close:
            self.performSelector_withObject_afterDelay_(
                "processDeferredCloses", None, 0.3)

    def finishExitedClose(self):
        title = self._pending_close_exit_title
        if not title:
            return
        self._pending_close_exit_title = None
        if ax.close_main_window(title):
            _geom_log("  finishExitedClose: closed %r's un-fullscreened window" % title)
            self._fullscreen_registry.pop(title, None)
            self._pending_chain_done()
        else:
            _geom_log("  finishExitedClose: could NOT close %r's window" % title)
            print("ExtraMissionControls: un-fullscreened %r but could not "
                  "close its window" % title)
            self._pending_chain_failed(title)
