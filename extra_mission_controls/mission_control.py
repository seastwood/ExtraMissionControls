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
        self._verify_title = None
        self._verify_element = None
        self._exit_close_title = None
        self._exit_close_attempts = 0
        # Remaining split members to close after the current pending close
        # (the ✕2 button closes every app of a Split View tile in sequence).
        self._after_close_queue = []
        # Split halves no-op the AX close press, so ✕2 chains skip straight
        # to the traffic-light click; solo fullscreen tiles keep press-first.
        self._chain_click_first = False
        # {app name: (window el, close el, ax title)} — windows
        # un-fullscreened in place (AXRemoveDesktop) whose close is
        # finished/re-verified once MC exits.
        self._deferred_close = {}
        # (kind, title) -> deadline; keeps a just-closed target's ✕ from
        # flashing back during Mission Control's re-layout animation.
        self._recently_closed = {}
        # [(action, title)] window closes/minimizes queued to run once Mission
        # Control is dismissed (closing in place leaves a ghost thumbnail).
        self._deferred_actions = []
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
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                _DETECT_INTERVAL, self, "tick:", None, True)
            # Let macOS coalesce the idle wake-ups with other system work.
            self._timer.setTolerance_(0.15)
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            self, "spaceChanged:", NSWorkspaceActiveSpaceDidChangeNotification, None)
        # Window sets only change around app activations and space switches,
        # so the fullscreen/window registry is refreshed on those events
        # instead of on a constant polling cadence.
        center.addObserver_selector_name_object_(
            self, "appActivated:",
            "NSWorkspaceDidActivateApplicationNotification", None)

    def stop(self):
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(self)
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
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
        if self._deferred_actions:
            # Window closes/minimizes clicked during Mission Control land now,
            # with Mission Control gone, so they leave no ghost thumbnail.
            self.performSelector_withObject_afterDelay_(
                "applyDeferredActions", None, 0.35)
        if self._deferred_close:
            # Apps expose AX again shortly after Mission Control closes;
            # finish/verify any in-place fullscreen closes then.
            self.performSelector_withObject_afterDelay_(
                "processDeferredCloses", None, 0.9)

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
                glyph = self._marked.get(thumb["title"])
                if glyph is not None:
                    marks.append((thumb, glyph))
                    continue
                targets.append(("window", self._slot(thumb, 0)))
                targets.append(("minimize",
                                self._slot(thumb, 1, "−", ui.HOVER_YELLOW)))
                targets.append(("makefullscreen",
                                self._slot(thumb, 2, "⤢", ui.HOVER_GREEN)))

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
        # kind == "window": close it. Closing (or minimizing) a windowed
        # thumbnail *while Mission Control is open* leaves a ghost thumbnail the
        # Dock never removes — clicking it reopens the app (verified). So defer
        # the action until Mission Control is dismissed, when it lands like an
        # ordinary close with no ghost.
        self._defer_window_action("close", title)

    @objc.python_method
    def _defer_window_action(self, action, title):
        """Queue a window close/minimize to run when Mission Control exits.
        The buttons vanish now and the thumbnail gets a dim '✕'/'−' scrim for
        the rest of the session, so it's clear what will act on exit and the
        buttons can never reappear on a lingering thumbnail."""
        if not any(t == title for _, t in self._deferred_actions):
            self._deferred_actions.append((action, title))
        # Show a dim "marked" overlay on this thumbnail instead of its buttons.
        self._marked[title] = "−" if action == "minimize" else "✕"
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
            else:
                ok = (ax.close_window_by_title(title, pids)
                      or self._press_held_window(title, infos))
            _geom_log("APPLY-DEFERRED %s %r -> %s" % (action, title, ok))

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
        silent no-op (verified) — there is no way to fullscreen a window while
        staying in Mission Control. So we AXPress the thumbnail — which focuses
        that window and leaves Mission Control (you end up looking at it) —
        then press ⌃⌘F, the system Enter Full Screen shortcut."""
        if element is None:
            return
        self._panels[index].orderOut_(None)
        self._rects.pop(index, None)
        _geom_log("MAKE-FULLSCREEN window=%r" % title)
        ax.press_element(element)  # focus the window, exit Mission Control
        self.performSelector_withObject_afterDelay_(
            "sendFullscreenShortcut", None, 0.6)

    def sendFullscreenShortcut(self):
        # ⌃⌘F = View → Enter Full Screen (system default shortcut).
        F_KEYCODE = 3
        flags = Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskCommand
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, F_KEYCODE, down)
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.03)

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
        entry = self._fullscreen_registry.get(title)
        _geom_log("CLOSE-FULLSCREEN %r: has_registry=%s" % (title, entry is not None))
        if os.environ.get("EMC_FORCE_UNFS") and entry is not None:
            # Test hook: pretend the direct close press no-ops, to exercise
            # the stay-in-MC AXRemoveDesktop path.
            self._panels[index].orderOut_(None)
            self._rects.pop(index, None)
            self._unfullscreen_and_close_held(title, element, entry)
            return
        pressed = entry is not None and ax.press_element(entry[1])
        if not pressed:
            # Held reference went stale; try a live lookup (works for apps
            # that report cross-space windows, e.g. Electron).
            pressed = ax.close_fullscreen_window(title)
        if pressed:
            # The press was delivered, but some apps no-op their close button
            # while fullscreen. Believe only the Spaces Bar: the tile
            # disappears once the window is really gone.
            self._recently_closed[("acted", title)] = time.time() + 2.5
            self._panels[index].orderOut_(None)
            self._rects.pop(index, None)
            self._verify_title = title
            self._verify_element = element
            self.performSelector_withObject_afterDelay_(
                "verifyFullscreenPress", None, 1.2)
            return
        self._fallback_close_fullscreen(title, element)

    def verifyFullscreenPress(self):
        title, element = self._verify_title, self._verify_element
        self._verify_title = None
        self._verify_element = None
        if not title:
            return
        if not any(s["title"] == title
                   for s in ax.mission_control_spaces()):
            self._fullscreen_registry.pop(title, None)  # really closed
            return
        _debug("fullscreen close press no-oped for %r" % title)
        entry = self._fullscreen_registry.get(title)
        if entry is not None:
            # Stay in Mission Control: have the Dock un-fullscreen the space
            # (AXRemoveDesktop), then close the now-normal window through the
            # held reference.
            self._unfullscreen_and_close_held(title, element, entry)
        else:
            self._recently_closed.pop(("acted", title), None)
            self._fallback_close_fullscreen(title, element)

    @objc.python_method
    def _unfullscreen_and_close_held(self, title, element, entry):
        if element is None or not ax.remove_space(element):
            self._recently_closed.pop(("acted", title), None)
            self._fallback_close_fullscreen(title, element)
            return
        _debug("AXRemoveDesktop on %r — un-fullscreened in place" % title)
        self._recently_closed[("acted", title)] = time.time() + 3.0
        self._fullscreen_registry.pop(title, None)
        self._deferred_close[title] = entry
        self._exit_close_title = title
        self._exit_close_attempts = 4
        self.performSelector_withObject_afterDelay_(
            "closeAfterExitFullscreen", None, 1.7)

    def closeAfterExitFullscreen(self):
        title = self._exit_close_title
        if not title:
            return
        entry = self._deferred_close.get(title)
        if entry is None:
            self._exit_close_title = None
            return
        window = entry[0]
        # The traffic lights are recreated when a window leaves fullscreen —
        # the held close-button ref is stale. Re-read it from the window.
        button = ax.close_button_of(window) or entry[1]
        if ax.press_element(button):
            _debug("pressed close on un-fullscreened %r" % title)
            self._exit_close_title = None
            # Entry stays in _deferred_close: verified once MC exits.
            return
        self._exit_close_attempts -= 1
        if self._exit_close_attempts > 0:
            # Transition still animating, or the app's AX is walled off
            # while MC is open; try again shortly.
            self.performSelector_withObject_afterDelay_(
                "closeAfterExitFullscreen", None, 1.0)
        else:
            self._exit_close_title = None
            _debug("in-MC close of %r failed; finishing after MC exits" % title)

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
        for title, entry in list(self._deferred_close.items()):
            self._deferred_close.pop(title, None)
            window, held_button, ax_title = entry
            # The window element itself can be recreated by the fullscreen
            # transition; re-find it now that the app's AX is visible again.
            if ax.element_alive(window):
                target = window
            else:
                target = ax.find_app_window(title, window, ax_title)
            if target is None:
                _debug("deferred close of %r: already gone" % title)
                continue
            button = ax.close_button_of(target) or held_button
            if ax.press_element(button):
                _debug("deferred close of %r completed" % title)
            else:
                print("ExtraMissionControls: could not finish closing %r"
                      % title)

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
