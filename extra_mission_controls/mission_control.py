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
    NSAnimationContext,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSPanel,
    NSPopUpMenuWindowLevel,
    NSScreen,
    NSTimer,
    NSWindowSharingNone,
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

_DETECT_INTERVAL = 0.15   # Mission Control open/close detection (was 0.25;
                          # tightened with its tolerance so the buttons appear
                          # sooner after MC opens — the check is one ~0.1ms
                          # CGWindowList query, so even ~7Hz is negligible)
_SYNC_INTERVAL = 0.1      # button re-positioning while MC is active
_HOVER_INTERVAL = 1 / 30.0  # mouse-position polling while MC is active
_FADE = 0.15              # button/tray/scrim fade in/out duration (seconds)
_QUIT_HOVER_DELAY = 0.4   # dwell on the quit button before its menu opens
_QUIT_HOVER_CLOSE_DELAY = 0.35  # grace before a hover-opened menu self-closes
_REGISTRY_MIN_GAP = 1.0   # coalesce event-driven registry scans
# A probable-exit latch may only hold this long. Real exits deactivate within
# ~0.3-0.7s of the trigger (measured), so a latch still alive with Mission
# Control up was a false positive (e.g. a click that dismissed nothing);
# expiring back to normal is safe because the scene gate below independently
# suppresses rendering whenever a transition is actually running.
_EXIT_LATCH_MAX = 0.8
# Settle confirmation (desktop gestures). macOS 26 animates the REAL windows
# during a desktop<->MC transition and the AX tree tracks the gesture LIVE, so
# a paused gesture is "still" — indistinguishable from settled by motion.
# Observed discriminator: the Spaces Bar tile stays SCALED-DOWN and off-screen
# (65x24 @ y<0) for the whole gesture including holds, and snaps to full size
# (138x90 @ y=46) exactly at commit — so the bar turning settled IS the commit
# signal, and a thumbs layout is only trusted then, or after it holds still
# for _SETTLE_CONFIRM (covers exit-side holds, where the bar stays expanded,
# and re-flows after closes/drags). _SETTLE_CONFIRM_NOBAR is the fallback for
# a hypothetical machine whose bar rests collapsed: a longer hold still opens.
_SETTLE_CONFIRM = 0.4
_SETTLE_CONFIRM_NOBAR = 1.5

# The scene gate opens after this many consecutive at-rest probe reads
# (30 Hz): the WindowServer transforms our windows during MC's enter/exit
# zooms, so "probe exactly where we put it, repeatedly" = the scene is fully
# settled and the buttons would really be clickable. Until then nothing is
# rendered and the Dock's AX tree isn't read at all. 3 ticks = 100ms of
# pixel-exact rest — a slowly dragged gesture never holds that.
_SCENE_STREAK_MIN = 3
# Fail-safes, tiered by what they mean. UNREADABLE (the probe vanished from
# the window list — the signal itself broke): force the gate open after ~3s.
# DIVERGENT (probe readable but transformed) is a healthy signal — a slow
# gesture can legitimately hold it for many seconds, so only a much longer
# stretch (~30s) forces the gate open, purely as insurance against a future
# macOS reporting bounds with a huge constant offset.
_SCENE_DEAD_TICKS_MAX = 90
_SCENE_CLOSED_TICKS_MAX = 900
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

# Flyout menu rows, top to bottom, parallel to the ui.*_MENU_ITEMS glyph lists.
# Quit menu: graceful ⌘Q, Force Quit (forceTerminate), a raw SIGKILL backstop.
# Arrange menu: left half / right half / maximize on the window's own screen.
_QUIT_MENU_ACTIONS = ("quit", "forcequit", "kill")
_ARRANGE_MENU_ACTIONS = ("snapleft", "snapright", "snapmax")


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
        self._fade_target = {}      # id(panel) -> 1/0 intended alpha, for fades
        self._buttons = []          # the CloseButton inside each panel
        self._targets = {}          # index -> (kind, title, space element|None)
        self._rects = {}            # index -> button rect, top-left coords
        self._geom_signature = None  # last-logged layout, to throttle dumps
        self._prev_layout = None      # last frame's positions, for motion detect
        self._screen_width = None
        self._active = False
        self._mc_group = None   # cached Dock 'mc' element while MC is up
        self._timer = None
        self._sync_timer = None
        self._hover_timer = None
        self._screen_height = None
        self._tap = None
        self._tap_source = None
        self._swallow_mouse_up = False
        self._swallow_right_up = False
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
        # {reappeared thumbnail title: app title} — a closed full-screen app
        # drops back to the desktop and its close is queued (by app name) in
        # _deferred_app_closes; this maps the scrimmed thumbnail back to that app
        # so a cancel (↺) on it can call the close off. _pending_unfs_app is the
        # app the current baseline is still watching for.
        self._pending_unfs_app = None
        self._unfs_pending = {}
        # Make-fullscreen (⤢) after MC exits: AX full-screen button, then a
        # green-button click fallback (the ⌃⌘F shortcut is ignored by
        # Electron/CEF apps). Staged across steps via these.
        self._fs_make_title = None
        self._fs_make_rect = None
        self._fs_make_owner = None
        # Arrange/tile actions applied one at a time after MC exits: focus the
        # window, wait for the app to come forward, press its native tile menu,
        # then move to the next. Staging keeps two windows' tile animations from
        # colliding (which left them mis-placed and un-paired). [(region, title)]
        self._arrange_queue = []
        self._arrange_current = None  # (title, pids, pid, region) mid-step
        # Probable-exit latch: an unconsumed left-click in Mission Control
        # almost always dismisses it, and the exit zoom is invisible in the AX
        # tree (it stays frozen at the settled layout until it vanishes). The
        # click hides all buttons; this latch keeps them hidden while the
        # layout still matches the click-time signature, so the sync loop
        # doesn't re-show them over the exit animation. A layout that moves on
        # proves Mission Control survived (a drag, the '+' button) — unlatch.
        self._exit_latched = False
        self._exit_latch_sig = None
        self._exit_latch_expires = 0.0
        self._activated_at = 0.0
        # Scene probe: one invisible (alpha-0) panel kept up for the whole
        # Mission Control session. The WindowServer applies MC's enter/exit
        # zoom transform to our windows, so the probe's CG bounds diverging
        # from the frame we set means the scene is MID-TRANSITION — and while
        # that is true nothing is clickable, so nothing is rendered and the
        # AX tree isn't even read. Buttons exist only at rest. _scene_streak
        # counts consecutive at-rest reads (30 Hz); >= _SCENE_STREAK_MIN opens
        # the gate. It initialises OPEN so headless tests (which never
        # activate) exercise the downstream logic; _activate arms it closed.
        self._scene_probe = None
        self._scene_probe_rect = None
        self._scene_streak = _SCENE_STREAK_MIN
        self._scene_dead_ticks = 0    # unreadable-probe ticks (signal broke)
        self._scene_closed_ticks = 0  # any closed-gate ticks (insurance)
        # Settled-layout state machine (desktop gestures): the thumbs layout
        # currently trusted for rendering, the candidate layout waiting to be
        # trusted, when it started holding still, and the bar's last settled
        # reading (its False->True transition is the gesture-commit signal).
        self._settled_thumbs_sig = None
        self._candidate_sig = None
        self._candidate_since = 0.0
        self._prev_bar_settled = False
        self._commit_until = 0.0  # brief fast-accept window after a gesture commits
        self._cg_sig = None       # real-window bounds signature (syncTick pulse)
        self._prev_cg_sig = None
        # {title: glyph} thumbnails marked for a deferred action — shown with a
        # dim overlay so it is clear which windows will close on exit.
        self._marked = {}
        self._mark_panels = []       # reusable dim-overlay pool
        self._tray_panels = []       # Liquid Glass tray behind each button row
        # Last state applied to each mark/tray panel, so identical frames at
        # 10 Hz skip the window moves (each forces an expensive glass redraw).
        self._mark_applied = {}
        self._tray_applied = {}
        self._perf_frames = []       # EMC_DEBUG per-frame (collect, panels) s
        # Flyout menu (the quit menu, or the arrange/tile menu): our own panel,
        # hit-tested through the same event tap as the buttons (an NSMenu can't
        # receive events while the Dock owns the mouse during Mission Control).
        self._menu_open = False
        self._menu_title = None       # the thumbnail the open menu acts on
        self._menu_rects = {}         # row index -> (x, y_top, w, h), screen
        self._menu_actions = ()       # action per row for the currently-open menu
        self._menu_panel = None
        self._menu_view = None
        # A flyout also opens by dwelling the cursor on its button — the quit or
        # the arrange button — not just clicking it; a hover-opened menu
        # self-closes when the cursor leaves the button and the menu. These
        # track that gesture.
        self._hover_quit_index = None   # menu button the cursor is dwelling on
        self._hover_quit_since = 0.0    # when the dwell began (0 = don't open)
        self._menu_via_hover = False    # menu opened by hover (so it self-closes)
        self._menu_away_since = 0.0     # when the cursor left the hover hot zone
        self._menu_anchor_rect = None   # quit button rect the open menu belongs to
        self._menu_panel_rect = None    # open menu panel rect (top-left coords)
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
            self._timer.setTolerance_(0.05)

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
        if self._active:
            # syncTick (10 Hz) owns liveness while Mission Control is up;
            # walking the Dock's tree here too would just double the AX load.
            return
        # Idle path: a single WindowServer query; only touch the Dock's
        # AX tree when Mission Control actually looks active.
        if ax.mission_control_probably_active():
            group = ax.mission_control_group()
            if group is not None:
                self._activate(group)
                return
        if self._last_registry_scan == 0.0:
            self._scan_registry()  # first scan / forced post-MC rescan

    @objc.python_method
    def _activate(self, group):
        self._active = True
        self._activated_at = time.time()
        self._mc_group = group  # cached; syncTick revalidates with one AX call
        # AX coordinates are relative to the primary screen's top-left.
        self._screen_height = NSScreen.screens()[0].frame().size.height
        self._screen_width = NSScreen.screens()[0].frame().size.width
        # Arm the scene gate closed and raise the invisible probe: nothing is
        # read or rendered until the probe proves the enter zoom has finished
        # and a thumbs layout earns trust (bar-commit or a confirmed hold).
        self._scene_streak = 0
        self._scene_dead_ticks = 0
        self._scene_closed_ticks = 0
        self._settled_thumbs_sig = None
        self._candidate_sig = None
        self._candidate_since = 0.0
        self._prev_bar_settled = False
        self._commit_until = 0.0
        self._cg_sig = None
        self._prev_cg_sig = None
        self._show_scene_probe()
        self._set_tap_enabled(True)
        self._start_hover_polling()
        self._sync_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            _SYNC_INTERVAL, self, "syncTick:", None, True)
        self._sync(group)

    @objc.python_method
    def _deactivate(self):
        _geom_log("DEACTIVATE %.3f (mc group gone)" % (time.time() % 1000))
        self._active = False
        self._mc_group = None
        self._scene_streak = 0
        if self._scene_probe is not None:
            self._scene_probe.orderOut_(None)
        if self._sync_timer is not None:
            self._sync_timer.invalidate()
            self._sync_timer = None
        self._set_tap_enabled(False)
        self._stop_hover_polling()
        # Instant, not faded: Mission Control is gone, so a fade would float
        # the buttons over whatever screen replaced it (the tail of the flash
        # when exiting into a full-screen app).
        self._hide_all(immediate=True)
        self._exit_latched = False   # the exit the latch predicted has landed
        self._exit_latch_sig = None
        self._prev_layout = None  # next open re-detects "still" from scratch
        self._last_registry_scan = 0.0  # spaces likely changed; rescan soon
        self._recently_closed = {}  # session-scoped suppression ends with MC
        self._marked = {}           # dim overlays clear with MC
        self._pending_unfs_baseline = None
        self._pending_unfs_app = None
        self._unfs_pending = {}
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
            # Cheapest check first: one WindowServer query, no Dock IPC. It
            # also yields the real-window bounds signature the settle machine
            # uses to spot a gesture commit (AX still + real windows easing).
            present, self._cg_sig = ax.mission_control_pulse(os.getpid())
            if not present:
                self._mc_group = None
                if ax.mission_control_group() is None:
                    self._deactivate()
                else:
                    # AX tree still up: MC is animating closed. The tree stays
                    # frozen at the settled layout through the whole exit zoom
                    # (verified via the GATE trace), so hide now rather than
                    # float buttons until the group finally vanishes.
                    _geom_log("EXIT-HIDE backdrop gone (group still up)")
                    self._hide_all(immediate=True)
                return
            if self._scene_streak < _SCENE_STREAK_MIN:
                # Mid-transition (the scene probe reads transformed): nothing
                # is clickable, nothing is rendered, and there is nothing to
                # read — this frame costs the one backdrop query above. The
                # probe itself is polled at 30 Hz by the hover tick, and
                # deactivation is covered by the backdrop check.
                return
            # Reuse the cached 'mc' group element: verifying it is alive is a
            # single AX call, where re-discovering it walks the Dock's whole
            # child list every frame.
            group = self._mc_group
            if group is None or not ax.element_alive(group):
                group = ax.mission_control_group()
                self._mc_group = group
            if group is None:
                self._deactivate()
                return
            self._sync(group)
        except Exception:
            traceback.print_exc()

    @objc.python_method
    def _sync(self, group):
        if not _DEBUG:
            targets, marks = self._collect_targets(group)
            self._sync_panels(targets)
            self._sync_marks(marks)
            return
        # EMC_DEBUG: split each frame's cost into the AX reads (collect) and
        # the AppKit panel work, aggregated every 50 frames into a PERF line.
        t0 = time.time()
        targets, marks = self._collect_targets(group)
        t1 = time.time()
        self._sync_panels(targets)
        self._sync_marks(marks)
        t2 = time.time()
        self._perf_frames.append((t1 - t0, t2 - t1))
        if len(self._perf_frames) >= 50:
            collect = [c for c, _ in self._perf_frames]
            panels = [p for _, p in self._perf_frames]
            _geom_log("PERF %d frames: collect avg=%.1fms max=%.1fms | "
                      "panels avg=%.2fms max=%.2fms"
                      % (len(self._perf_frames),
                         1000 * sum(collect) / len(collect),
                         1000 * max(collect),
                         1000 * sum(panels) / len(panels),
                         1000 * max(panels)))
            self._perf_frames = []

    # -- fullscreen-window registry --------------------------------------------

    def spaceChanged_(self, notification):
        if self._active and time.time() - self._activated_at > 1.2:
            # The active space changed while Mission Control is up: we are
            # leaving it (a tile/thumbnail click, a swipe to another space).
            # Hide instantly — the AX tree won't show the exit until it's over.
            # The grace period skips the space change macOS itself makes while
            # OPENING Mission Control from a fullscreen space (it swaps to the
            # desktop to show the overview) — latching on that one would hide
            # the buttons for the whole session.
            _geom_log("EXIT-HIDE space changed")
            self._latch_exit()
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
        # Scene gate: while Mission Control's enter/exit zoom is running (the
        # invisible probe reads transformed — see _check_scene_transform),
        # nothing on screen is where AX says it is and nothing is clickable,
        # so render nothing and skip even the AX reads. Buttons exist only
        # once the scene is at rest.
        if self._scene_streak < _SCENE_STREAK_MIN:
            self._trace_gate("scene-transforming", 0, [], [])
            return [], []
        now = time.time()
        self._recently_closed = {key: deadline
                                 for key, deadline in self._recently_closed.items()
                                 if deadline > now}
        # One batched walk for everything this frame needs — thumbnails, bar
        # tiles, and the mc.windows child count — instead of three.
        thumbs, spaces, window_count = ax.mission_control_snapshot(group)
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
                        if self._pending_unfs_app is not None:
                            self._unfs_pending[title] = self._pending_unfs_app
                        _geom_log("UNFS-SCRIM: %r reappeared -> marked" % title)
            else:
                self._pending_unfs_baseline = None
                self._pending_unfs_app = None
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
            self._trace_gate("baseline", window_count, spaces, thumbs)
            return [], []  # first frame after (re)activation: baseline
        spaces_still = spaces_sig == prev[0]
        thumbs_still = thumbs_sig == prev[1]

        # Probable-exit latch: keep everything hidden while the layout still
        # matches its latch-time signature (the tree freezes there for the
        # whole exit animation). Movement proves MC survived the trigger, and
        # so does time: every real exit deactivates within _EXIT_LATCH_MAX, so
        # an expired latch was a false positive (most often the ENTRY zoom —
        # the tree settles before the animation ends, so freshly placed
        # buttons read as transformed and latch; without the expiry they would
        # stay hidden until the layout next moved). A latch taken before any
        # frame existed adopts the first layout it sees, so it can always be
        # released — never a permanent hide.
        if self._exit_latched:
            sig = (spaces_sig, thumbs_sig)
            if self._exit_latch_sig is None:
                self._exit_latch_sig = sig
            if sig == self._exit_latch_sig and now < self._exit_latch_expires:
                self._trace_gate("exit-latched", window_count, spaces, thumbs)
                return [], []
            self._exit_latched = False
            self._exit_latch_sig = None
            self._trace_gate("exit-latch released", window_count, spaces, thumbs)

        # Settle confirmation. macOS 26 animates the REAL windows through a
        # desktop<->MC gesture and the AX tree tracks it LIVE, so a paused
        # gesture reads as perfectly "still" — motion detection alone would
        # render buttons mid-hold (and at positions that JUMP at commit).
        # Observed commit signal: the Spaces Bar tile stays scaled-down and
        # off-screen (65x24 @ y<0) throughout the gesture, snapping to full
        # size (138x90 @ y=46) exactly when the gesture commits. A thumbs
        # layout is therefore only trusted when the bar just turned settled
        # (the commit), or after holding still for _SETTLE_CONFIRM (exit-side
        # holds and post-close re-flows; _SETTLE_CONFIRM_NOBAR covers any
        # machine whose bar rests collapsed).
        bar_ok = self._bar_settled(spaces)
        if bar_ok and not self._prev_bar_settled \
                and self._settled_thumbs_sig is None:
            # The Spaces Bar snapping to full size is the open gesture
            # committing. The thumbnails jump to their final spots in that SAME
            # frame, so bar_committed lands where thumbs_still is False and
            # can't be trusted yet — open a short window instead and accept the
            # first still layout inside it, so buttons appear ~1 frame after
            # commit rather than waiting out _SETTLE_CONFIRM. Scoped to the
            # first bar-settle of the session (an entry); an in-MC bar hover or
            # a post-close re-flow still takes the normal confirmation.
            self._commit_until = now + 0.5
        self._prev_bar_settled = bar_ok
        # The bar only expands (and so only signals commit) when the cursor is
        # near the top of the screen. The cursor-independent commit signal:
        # macOS moves the REAL windows through the gesture with the AX tree
        # tracking them LIVE, but at commit the tree SNAPS to the final layout
        # while the windows visibly ease after it — so "AX still, real windows
        # moving" happens at exactly one moment, the commit. A held gesture is
        # frozen in both; a drag moves both together.
        cg_moving = (self._prev_cg_sig is not None
                     and self._cg_sig != self._prev_cg_sig)
        self._prev_cg_sig = self._cg_sig
        if cg_moving and thumbs_still and self._settled_thumbs_sig is None:
            _geom_log("COMMIT-EASE detected (AX still, windows easing)")
            self._commit_until = now + 0.5
        if thumbs_still and thumbs_sig != self._settled_thumbs_sig:
            if self._candidate_sig != thumbs_sig:
                self._candidate_sig = thumbs_sig
                self._candidate_since = now
            confirm = _SETTLE_CONFIRM if bar_ok else _SETTLE_CONFIRM_NOBAR
            if now < self._commit_until \
                    or now - self._candidate_since >= confirm:
                self._settled_thumbs_sig = thumbs_sig
                self._candidate_sig = None
                self._commit_until = 0.0
        elif not thumbs_still:
            self._candidate_sig = None
        mc_settled = thumbs_still and thumbs_sig == self._settled_thumbs_sig
        if not mc_settled:
            self._trace_gate("await-settle bar_ok=%d still=%d"
                             % (bar_ok, thumbs_still),
                             window_count, spaces, thumbs)
            return [], []

        # A window grown to nearly fill the screen means Mission Control is
        # zooming into or out of a full-screen space (opening from it, or closing
        # back into it). The whole layout is mid-flight then, and its ease-out
        # tail can round to "still" for a frame or two — exactly when a stray
        # button flashes on, on the zooming window OR on the still Spaces Bar,
        # right before MC finishes. Suppress every button until the transition
        # ends: MC closes, or an overview settles with its thumbnails scaled back
        # down (a real overview thumbnail never approaches full-screen size).
        if any(self._zooming(t) for t in thumbs):
            self._trace_gate("zoom-suppress", window_count, spaces, thumbs)
            return [], []

        # Distinguish an empty desktop overview from a fullscreen/split-space
        # view — the two states are otherwise identical in the AX tree, but in
        # the fullscreen view the whole subtree (bar and preview) reports stale
        # desktop-overview coordinates, so there is no reliable place for a ✕.
        # The tell: mc.windows is empty on a desktop, but has a (frame-less,
        # unreadable) child for the fullscreen space's window. Suppress there;
        # fullscreen apps are closed from a desktop overview's bar tiles.
        on_screen = [t for t in thumbs if self._on_screen(t)]
        if not on_screen and window_count > 0:
            self._trace_gate("fs-suppress", window_count, spaces, thumbs)
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
                if self._untitled(thumb):
                    # A titleless thumbnail is a tooltip/popup Mission Control
                    # caught mid-hover (e.g. an IDE hover tip on screen as MC
                    # opened), not a window anyone manages — and every action
                    # here is keyed by title, so buttons on it could never act
                    # on the right thing. No buttons.
                    continue
                glyph = self._mark_glyph_for(thumb["title"])
                if glyph is not None:
                    marks.append((thumb, glyph))
                    # The pending action can be taken back: float a cancel (↺)
                    # button over the scrim. Click it to clear the mark and get
                    # the buttons back — to pick a different action or none at
                    # all. This covers a deferred window action, and the close
                    # queued for a full-screen app that has just dropped back to
                    # the desktop as this thumbnail (cancel keeps it open, though
                    # it has already left full screen).
                    if (self._deferred_action_for(thumb["title"]) is not None
                            or thumb["title"] in self._unfs_pending):
                        targets.append(("cancel",
                                        self._slot(thumb, 0, "↺", ui.HOVER_BLUE)))
                    continue
                targets.append(("window", self._slot(thumb, 0)))
                targets.append(("minimize",
                                self._slot(thumb, 1, "−", ui.HOVER_YELLOW)))
                targets.append(("makefullscreen",
                                self._slot(thumb, 2, "⤢", ui.HOVER_GREEN)))
                targets.append(("arrange",
                                self._slot(thumb, 3, "◫", ui.HOVER_TEAL)))
                targets.append(("quit",
                                self._slot(thumb, 4, "⏻", ui.HOVER_PURPLE)))

        # Bar-tile buttons only exist while the bar is still AND expanded at the
        # top of the screen; collapsed tiles sit above the screen edge (negative
        # y) so there is nowhere to draw them until the user hovers the bar open.
        # Ordered top-left: ✕ close (red) · ❏ exit full screen (green). A
        # fullscreen space cannot be minimized, so no minimize button here.
        #
        # Also require the window layer to be still: swiping between spaces
        # inside Mission Control pans the thumbnails while the bar holds put, so
        # the bar tiles pass spaces_still mid-swipe and the buttons would flash
        # on before the new overview settles. Waiting for thumbs_still too holds
        # them back until the pan finishes (a settled overview has both still).
        if not (spaces_still and thumbs_still and self._bar_settled(spaces)):
            self._trace_gate("bar-gated emit W=%d sp_still=%d th_still=%d"
                             % (len(targets), spaces_still, thumbs_still),
                             window_count, spaces, thumbs)
            return targets, marks
        for space in spaces:
            if not self._on_screen(space):
                continue
            if ("acted", space["title"]) in self._recently_closed:
                continue
            glyph = self._mark_glyph_for(space["title"])
            if glyph is not None:  # queued for a deferred close → dim scrim
                marks.append((space, glyph))
                # A held-reference full-screen close still sits in the Spaces
                # Bar (it never left full screen), so it is fully reversible —
                # give it the same cancel (↺) button.
                if space["title"] in self._deferred_close:
                    targets.append(("cancel",
                                    self._slot(space, 0, "↺", ui.HOVER_BLUE)))
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
        self._trace_gate("full emit n=%d" % len(targets), window_count, spaces, thumbs)
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
    def _deferred_action_for(self, title):
        """The pending (action, title) queued for this thumbnail, or None.
        Matched exactly or by the same unique-prefix relation _mark_glyph_for
        uses, so the cancel button lines up with the scrim even when the Dock
        shows a shortened title. Only window actions (_defer_window_action) live
        in _deferred_actions, so this is None for fullscreen-tile scrims."""
        exact = [(a, t) for a, t in self._deferred_actions if t == title]
        if exact:
            return exact[0]
        related = [(a, t) for a, t in self._deferred_actions
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
    def _zooming(self, tile):
        """True when a window thumbnail is nearly the size of the whole screen —
        the transient zoom as Mission Control opens from, or closes into, a
        full-screen window. Real overview thumbnails are always scaled well below
        full size (the Spaces Bar and margins take room; a single window caps
        around three-quarters of the screen), so a thumbnail this large is an
        animation frame, not a place for a button. Suppressing it stops the
        buttons flashing on the window as it zooms in/out of full screen."""
        w = self._screen_width or 0.0
        h = self._screen_height or 0.0
        if w <= 0.0 or h <= 0.0:
            return False
        # 0.85 in both dimensions: a real overview thumbnail caps around
        # three-quarters of the screen, so this only trips during the zoom, and
        # the extra margin catches the ease-out tail a touch before it completes.
        return tile["width"] >= 0.85 * w and tile["height"] >= 0.85 * h

    @objc.python_method
    def _untitled(self, tile):
        """True for a thumbnail with no (or whitespace) title. Mission Control
        gives tooltip/popup windows their own thumbnail if one was on screen as
        it opened (an IDE hover tip, say) — untitled, because such windows have
        no window title. They are not manageable windows, and every EMC action
        resolves its window BY title, so buttons on one could never act on the
        right thing (worst case, an empty title mis-matching another untitled
        window). They get no buttons."""
        return not (tile.get("title") or "").strip()

    @objc.python_method
    def _trace_gate(self, decision, wc, spaces, thumbs):
        """One line per sync frame naming which gate decided (EMC_DEBUG only) —
        for chasing transient flashes: which gate let buttons through, on what
        geometry, in the frames right before Mission Control tears down."""
        if not _DEBUG:
            return
        pa = ax.mission_control_probably_active()  # backdrop still up?
        big = max(thumbs, key=lambda t: t["width"] * t["height"], default=None)
        bar = ("bar(y=%.0f h=%.0f n=%d)"
               % (spaces[0]["y"], spaces[0]["height"], len(spaces))
               if spaces else "bar(none)")
        thumb = ("thumb(max %.0fx%.0f@%.0f,%.0f n=%d)"
                 % (big["width"], big["height"], big["x"], big["y"], len(thumbs))
                 if big else "thumb(none)")
        _geom_log("GATE %.3f wc=%d pa=%d %s %s -> %s"
                  % (time.time() % 1000, wc, pa, bar, thumb, decision))

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
                    | Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseUp)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseUp))
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
            self._swallow_right_up = False
            self._close_menu()

    @objc.python_method
    def _tap_callback(self, proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            if self._active:
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        location = Quartz.CGEventGetLocation(event)
        x, y = location.x, location.y
        if event_type == Quartz.kCGEventLeftMouseDown:
            if self._menu_open:
                # A click while the quit menu is up: pick its row, or dismiss
                # (like a real menu, the dismiss click is consumed).
                self._swallow_mouse_up = True
                row = self._menu_row_at(x, y)
                if row is not None:
                    AppHelper.callAfter(self._menu_pick, row)
                else:
                    AppHelper.callAfter(self._close_menu)
                return None
            index = self._button_index_at(x, y)
            if index is not None:
                self._swallow_mouse_up = True
                if self._targets.get(index, (None,))[0] == "arrange":
                    # The arrange button has no default action — a left-click
                    # opens its Left/Right flyout (like a menu button).
                    AppHelper.callAfter(self._open_menu_for, index)
                else:
                    AppHelper.callAfter(self._close_index, index)
                return None  # consumed: don't let Mission Control see it
            # A left-click anywhere else in Mission Control dismisses it (a
            # thumbnail, a bar tile, the background) or starts a drag. The exit
            # zoom begins before the AX tree or the backdrop show any change,
            # so hide the buttons NOW, with the click, and LATCH: the sync loop
            # would otherwise re-show them next frame, since the tree stays
            # frozen at the settled layout through the whole exit animation.
            # If Mission Control survives (a drag, the '+' button), the layout
            # moves and the latch releases. The event passes through untouched.
            _geom_log("EXIT-HIDE click at (%.0f,%.0f)" % (x, y))
            AppHelper.callAfter(self._latch_exit)
        elif event_type == Quartz.kCGEventRightMouseDown:
            if self._menu_open:
                self._swallow_right_up = True
                AppHelper.callAfter(self._close_menu)
                return None
            index = self._button_index_at(x, y)
            if index is not None and \
                    self._targets.get(index, (None,))[0] in ("quit", "arrange"):
                # Right-click the quit or arrange button → its flyout menu.
                # Other buttons ignore right-clicks (event passes through).
                self._swallow_right_up = True
                AppHelper.callAfter(self._open_menu_for, index)
                return None
        elif event_type == Quartz.kCGEventLeftMouseUp and self._swallow_mouse_up:
            self._swallow_mouse_up = False
            return None
        elif event_type == Quartz.kCGEventRightMouseUp and self._swallow_right_up:
            self._swallow_right_up = False
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
        self._hover_quit_index = None
        self._hover_quit_since = 0.0

    def hoverTick_(self, timer):
        self._check_scene_transform()
        location = NSEvent.mouseLocation()  # global, bottom-left origin
        x = location.x
        y_top = self._screen_height - location.y
        if self._menu_open:
            # Highlight the quit menu's hovered row; keep the buttons quiet.
            row = self._menu_row_at(x, y_top)
            if self._menu_view is not None:
                self._menu_view.set_highlight(row if row is not None else -1)
            for button in self._buttons:
                button.setHovered_(False)
            self._maybe_autoclose_menu(x, y_top)
            return
        hit = self._button_index_at(x, y_top)
        for index, button in enumerate(self._buttons):
            button.setHovered_(index == hit)
        self._update_quit_hover(hit)

    @objc.python_method
    def _update_quit_hover(self, hit):
        """Open a button's flyout when the cursor dwells on it — the quit menu
        for the quit button, the arrange menu for the arrange button — so both
        are reachable by hovering as well as by clicking. Merely passing over
        the button does nothing; the cursor has to rest there for
        _QUIT_HOVER_DELAY."""
        has_menu = (hit is not None
                    and self._targets.get(hit, (None,))[0] in ("quit", "arrange"))
        if not has_menu:
            self._hover_quit_index = None
            self._hover_quit_since = 0.0
            return
        if self._hover_quit_index != hit:
            self._hover_quit_index = hit   # just arrived; start the dwell clock
            self._hover_quit_since = time.time()
            return
        # Same button as last tick. A zeroed clock means the dwell already fired
        # (or is suppressed just after a close) — wait for the cursor to leave
        # (hit changes) before it can open again.
        if (self._hover_quit_since
                and time.time() - self._hover_quit_since >= _QUIT_HOVER_DELAY):
            self._hover_quit_since = 0.0
            self._open_menu_for(hit, via_hover=True)

    @objc.python_method
    def _maybe_autoclose_menu(self, x, y):
        """A hover-opened menu closes itself once the cursor rests away from
        both the quit button and the menu for a moment. Right-click menus are
        left untouched — dismissed by a click, as before."""
        if not self._menu_via_hover:
            return
        if self._menu_hot(x, y):
            self._menu_away_since = 0.0
            return
        if not self._menu_away_since:
            self._menu_away_since = time.time()
        elif time.time() - self._menu_away_since >= _QUIT_HOVER_CLOSE_DELAY:
            self._close_menu()

    @objc.python_method
    def _menu_hot(self, x, y, slop=6.0):
        """True while the cursor is over the open menu or the quit button it
        belongs to — with a little slop to bridge the 2px gap between them — so
        travelling from the button into the menu counts as staying inside."""
        for rect in (self._menu_anchor_rect, self._menu_panel_rect):
            if rect is None:
                continue
            rx, ry, rw, rh = rect
            if rx - slop <= x <= rx + rw + slop \
                    and ry - slop <= y <= ry + rh + slop:
                return True
        return False

    # -- flyout menus (quit / arrange) -----------------------------------------
    # The quit button (hover-dwell or right-click) and the arrange button
    # (click) open a small list menu, drawn as our own panel and hit-tested
    # through the event tap because the Dock owns the mouse while Mission Control
    # is up (an NSMenu wouldn't work).

    @objc.python_method
    def _open_menu_for(self, index, via_hover=False):
        """Open the flyout that belongs to the button at `index`: the quit menu
        for a quit button, the arrange menu for an arrange button."""
        kind = self._targets.get(index, (None,))[0]
        if kind == "quit":
            self._open_menu(index, "quit", ui.QUIT_MENU_ITEMS,
                            _QUIT_MENU_ACTIONS, via_hover)
        elif kind == "arrange":
            self._open_menu(index, "arrange", ui.ARRANGE_MENU_ITEMS,
                            _ARRANGE_MENU_ACTIONS, via_hover)

    @objc.python_method
    def _open_menu(self, index, kind, items, actions, via_hover=False):
        entry = self._targets.get(index)
        rect = self._rects.get(index)
        if entry is None or rect is None or entry[0] != kind:
            return
        title = entry[1]
        bx, by, bw, bh = rect
        width = ui.MENU_WIDTH
        height = ui.menu_height(items)
        sw = self._screen_width or 0.0
        sh = self._screen_height or 0.0
        # Anchor the menu just under the button, kept fully on screen.
        mx = max(2.0, min(bx, sw - width - 2.0))
        my_top = by + bh + 2.0
        if my_top + height > sh:
            my_top = max(2.0, by - height - 2.0)
        self._menu_rects = {
            i: (mx, my_top + ui.MENU_PAD + i * ui.MENU_ROW_HEIGHT,
                width, ui.MENU_ROW_HEIGHT)
            for i in range(len(actions))
        }
        self._menu_title = title
        self._menu_actions = actions
        self._menu_anchor_rect = (bx, by, bw, bh)
        self._menu_panel_rect = (mx, my_top, width, height)
        self._menu_via_hover = via_hover
        self._menu_away_since = 0.0
        panel = self._menu_panel_ensure()
        self._menu_view.set_items(items)
        self._menu_view.set_highlight(-1)
        y = sh - my_top - height  # flip to bottom-left origin
        panel.setFrame_display_(NSMakeRect(mx, y, width, height), True)
        self._menu_view.setFrame_(NSMakeRect(0, 0, width, height))
        self._menu_open = True
        panel.orderFrontRegardless()
        _geom_log("MENU(%s) open for %r at (%.0f,%.0f)" % (kind, title, mx, my_top))

    @objc.python_method
    def _menu_panel_ensure(self):
        if self._menu_panel is None:
            width = ui.MENU_WIDTH
            height = ui.menu_height(ui.QUIT_MENU_ITEMS)  # any; resized per open
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, width, height),
                NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered, False)
            panel.setReleasedWhenClosed_(False)
            panel.setSharingType_(NSWindowSharingNone)  # not in MC snapshots
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(True)  # a menu casts a shadow, unlike the buttons
            panel.setHidesOnDeactivate_(False)
            # One level above the button panels so it overlays them like a menu.
            panel.setLevel_(NSPopUpMenuWindowLevel + 1)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorIgnoresCycle)
            root, menu_view = ui.make_menu(NSMakeRect(0, 0, width, height))
            panel.setContentView_(root)
            self._menu_panel = panel
            self._menu_view = menu_view
        return self._menu_panel

    @objc.python_method
    def _menu_row_at(self, x, y):
        for i, (rx, ry, rw, rh) in self._menu_rects.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return i
        return None

    @objc.python_method
    def _menu_pick(self, row):
        actions = self._menu_actions or ()
        action = actions[row] if 0 <= row < len(actions) else None
        title = self._menu_title
        self._close_menu()
        if action and title:
            self._defer_window_action(action, title)

    @objc.python_method
    def _close_menu(self):
        if not self._menu_open and self._menu_panel is None:
            return
        self._menu_open = False
        self._menu_title = None
        self._menu_rects = {}
        self._menu_via_hover = False
        self._menu_away_since = 0.0
        self._menu_anchor_rect = None
        self._menu_panel_rect = None
        # Suppress an immediate hover-reopen: keep the dwell anchored to this
        # button but clear its clock, so the menu can reopen only after the
        # cursor leaves the button and comes back.
        self._hover_quit_since = 0.0
        if self._menu_view is not None:
            self._menu_view.set_highlight(-1)
        if self._menu_panel is not None:
            self._menu_panel.orderOut_(None)

    # -- panel management ------------------------------------------------------

    @objc.python_method
    def _sync_panels(self, targets):
        trays = {}
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
            rect = (x, y_top, size, size)
            # In a settled overview the layout is identical frame to frame:
            # skip the window move (setFrame with display forces a redraw —
            # at 10 Hz across ~30 panels that alone was a steady CPU drain).
            # The glyph/hover setters below no-op internally on same values.
            moved = self._rects.get(index) != rect or not panel.isVisible()
            self._rects[index] = rect
            self._targets[index] = (kind, tile["title"], tile.get("element"))
            # Grow this tile's tray box to enclose the button. Buttons of one
            # tile share its title and top-left, so they group under one tray.
            key = (tile["title"], round(tile["x"]), round(tile["y"]))
            box = trays.get(key)
            if box is None:
                trays[key] = [x, y_top, x + size, y_top + size]
            else:
                box[0], box[1] = min(box[0], x), min(box[1], y_top)
                box[2], box[3] = max(box[2], x + size), max(box[3], y_top + size)
            if moved:
                y = self._screen_height - y_top - size  # flip to bottom-left
                panel.setFrame_display_(NSMakeRect(x, y, size, size), True)
            button.setLabel_(tile.get("label", "✕"))
            button.setHoverRGB_(tile.get("hover", ui.HOVER_RED))
            if abs(button.frame().size.width - size) > 0.5:
                button.setDiameter_(size)
            if not panel.isVisible():
                _debug("panel %d (%s %r) shown at (%.0f,%.0f) size=%.0f"
                       % (index, kind, tile["title"], x, y_top, size))
            self._show_panel(panel)
        for index in range(len(targets), len(self._panels)):
            self._hide_panel(self._panels[index])
            self._targets.pop(index, None)
            self._rects.pop(index, None)
        self._sync_trays(list(trays.values()))

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
        # Excluded from Mission Control's space-thumbnail captures: a stationary
        # all-spaces panel is otherwise composited into a full-screen space's
        # snapshot, so the buttons look baked into that tile in the Spaces Bar.
        panel.setSharingType_(NSWindowSharingNone)
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
        panel.contentView().addSubview_(button.outerView())
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
            state = (round(tile["x"], 1), round(tile["y"], 1),
                     round(w, 1), round(h, 1), glyph)
            # Settled frames repeat identically at 10 Hz — only touch the
            # window (a forced redraw) when the scrim actually moved/changed.
            if self._mark_applied.get(index) != state or not panel.isVisible():
                self._mark_applied[index] = state
                y = self._screen_height - tile["y"] - h  # flip to bottom-left
                panel.setFrame_display_(NSMakeRect(tile["x"], y, w, h), True)
                view = panel.contentView()
                if (abs(view.frame().size.width - w) > 0.5
                        or abs(view.frame().size.height - h) > 0.5):
                    view.setFrame_(NSMakeRect(0, 0, w, h))
                    view.setNeedsDisplay_(True)
                view.setGlyph_(glyph)
            self._show_panel(panel)
        for index in range(len(marks), len(self._mark_panels)):
            self._hide_panel(self._mark_panels[index])
            self._mark_applied.pop(index, None)

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
        # Excluded from Mission Control's space-thumbnail captures: a stationary
        # all-spaces panel is otherwise composited into a full-screen space's
        # snapshot, so the buttons look baked into that tile in the Spaces Bar.
        panel.setSharingType_(NSWindowSharingNone)
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

    # -- Liquid Glass tray behind each tile's button row -----------------------

    @objc.python_method
    def _sync_trays(self, boxes):
        """A Liquid Glass tray behind each tile's row of buttons, grouping them
        against busy thumbnails. `boxes` are [x0, y0, x1, y1] button-row bounds
        in top-left screen coords, grown by a small pad."""
        pad = 3.0
        for i, (x0, y0, x1, y1) in enumerate(boxes):
            tx, ty = x0 - pad, y0 - pad
            tw, th = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
            panel = self._tray_panel_at(i)
            state = (round(tx, 1), round(ty, 1), round(tw, 1), round(th, 1))
            # Vibrancy/glass redraws are the most expensive part of a frame;
            # skip the window move entirely while the tray box is unchanged.
            if self._tray_applied.get(i) != state or not panel.isVisible():
                self._tray_applied[i] = state
                y = self._screen_height - ty - th  # flip to bottom-left origin
                panel.setFrame_display_(NSMakeRect(tx, y, tw, th), True)
            self._show_panel(panel)
        for i in range(len(boxes), len(self._tray_panels)):
            self._hide_panel(self._tray_panels[i])
            self._tray_applied.pop(i, None)

    @objc.python_method
    def _tray_panel_at(self, index):
        while len(self._tray_panels) <= index:
            self._tray_panels.append(self._make_tray_panel())
        return self._tray_panels[index]

    @objc.python_method
    def _make_tray_panel(self):
        size = ui.CLOSE_BUTTON_SIZE
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, size, size),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setReleasedWhenClosed_(False)
        # Excluded from Mission Control's space-thumbnail captures: a stationary
        # all-spaces panel is otherwise composited into a full-screen space's
        # snapshot, so the buttons look baked into that tile in the Spaces Bar.
        panel.setSharingType_(NSWindowSharingNone)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(True)  # decorative; clicks fall through
        # One level below the buttons (so it never covers them) but above the
        # Mission Control backdrop.
        panel.setLevel_(NSPopUpMenuWindowLevel - 1)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorIgnoresCycle)
        panel.setContentView_(ui.make_tray(NSMakeRect(0, 0, size, size), 9.0))
        return panel

    # -- fade panels in / out --------------------------------------------------

    @objc.python_method
    def _show_panel(self, panel):
        """Fade a panel in. Idempotent once shown; also re-shows a panel that
        was hidden out of band (raw orderOut) or is mid fade-out."""
        if panel.isVisible() and self._fade_target.get(id(panel)) == 1:
            return
        self._fade_target[id(panel)] = 1
        if not panel.isVisible():
            panel.setAlphaValue_(0.0)
            panel.orderFrontRegardless()
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE)
        panel.animator().setAlphaValue_(1.0)
        NSAnimationContext.endGrouping()

    @objc.python_method
    def _hide_panel(self, panel):
        """Fade a panel out, then order it out — unless a later _show_panel
        cancels the pending fade (checked in _finish_hide)."""
        if self._fade_target.get(id(panel), 0) == 0:
            return  # already hidden or already fading out
        self._fade_target[id(panel)] = 0
        if not panel.isVisible():
            panel.orderOut_(None)
            return
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE)
        NSAnimationContext.currentContext().setCompletionHandler_(
            lambda: self._finish_hide(panel))
        panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

    @objc.python_method
    def _finish_hide(self, panel):
        if self._fade_target.get(id(panel), 0) == 0:
            panel.orderOut_(None)

    @objc.python_method
    def _bounds_match(self, number, ex, ey, ew, eh):
        """Compare a window's CG-reported bounds to the frame we set: True /
        False, or None when the window has no on-screen entry. A 5px threshold
        absorbs the WindowServer's small constant reporting offset; the zoom
        transform moves panels by tens to hundreds of pixels per frame."""
        raw = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, number) or []
        for info in raw:
            bounds = dict(info.get(Quartz.kCGWindowBounds) or {})
            if not bounds:
                return None
            return (abs(bounds.get("X", ex) - ex) <= 5.0
                    and abs(bounds.get("Y", ey) - ey) <= 5.0
                    and abs(bounds.get("Width", ew) - ew) <= 5.0
                    and abs(bounds.get("Height", eh) - eh) <= 5.0)
        return None

    @objc.python_method
    def _show_scene_probe(self):
        """Raise the invisible scene probe: a SCREEN-SIZED alpha-0 panel. The
        WindowServer applies Mission Control's enter/exit zoom to our windows,
        so while the scene is even slightly off rest the probe's CG bounds
        diverge from this frame. Screen-sized on purpose: a slow three-finger
        drag spends a long stretch within a few percent of identity, and a
        small probe read as "at rest" there — opening the gate mid-gesture and
        churning. At full size a 1%% zoom already moves the edges ~15px, well
        past the 5px threshold, so the gate only opens when Mission Control is
        genuinely settled. Alpha 0 keeps it invisible (nothing is composited)
        yet still CG-listed with live bounds (verified)."""
        w = float(self._screen_width or 1000)
        h = float(self._screen_height or 800)
        if self._scene_probe is None:
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, w, h),
                NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered, False)
            panel.setReleasedWhenClosed_(False)
            panel.setSharingType_(NSWindowSharingNone)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(False)
            panel.setHidesOnDeactivate_(False)
            panel.setIgnoresMouseEvents_(True)
            panel.setAlphaValue_(0.0)
            panel.setLevel_(NSPopUpMenuWindowLevel)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorIgnoresCycle)
            self._scene_probe = panel
        self._scene_probe_rect = (0.0, 0.0, w, h)
        self._scene_probe.setFrame_display_(NSMakeRect(0, 0, w, h), False)
        self._scene_probe.orderFrontRegardless()

    @objc.python_method
    def _check_scene_transform(self):
        """30 Hz while Mission Control is up: poll the scene probe and keep
        _scene_streak = consecutive at-rest reads. The gate in _collect_targets
        renders nothing (and reads no AX) until the streak clears
        _SCENE_STREAK_MIN, so buttons only ever exist when the scene is stable
        enough to click — never during the enter/exit zooms or a gesture
        paused half-way in. The moment a transform starts under visible
        buttons (an exit beginning), they are hidden in this same tick."""
        if not self._active or self._scene_probe is None \
                or self._scene_probe_rect is None:
            return
        x, y_top, w, h = self._scene_probe_rect
        match = self._bounds_match(self._scene_probe.windowNumber(),
                                   x, y_top, w, h)
        if match:
            self._scene_streak += 1
            self._scene_dead_ticks = 0
            self._scene_closed_ticks = 0
            return
        was_open = self._scene_streak >= _SCENE_STREAK_MIN
        self._scene_streak = 0
        if match is False and was_open and self._rects:
            # Exit (or a new transition) beginning under visible buttons.
            _geom_log("SCENE-TRANSFORM under visible buttons: hide")
            self._hide_all(immediate=True)
        # Tiered fail-safes. A DIVERGENT read is the signal working (a slow
        # gesture may hold it for many seconds — that must NOT force the gate
        # open mid-drag); only an UNREADABLE probe means the signal broke.
        if match is None:
            self._scene_dead_ticks += 1
        else:
            self._scene_dead_ticks = 0
        self._scene_closed_ticks += 1
        if (self._scene_dead_ticks >= _SCENE_DEAD_TICKS_MAX
                or self._scene_closed_ticks >= _SCENE_CLOSED_TICKS_MAX):
            _geom_log("SCENE-PROBE fail-safe: forcing gate open "
                      "(dead=%d closed=%d)"
                      % (self._scene_dead_ticks, self._scene_closed_ticks))
            self._scene_streak = _SCENE_STREAK_MIN
            self._scene_dead_ticks = 0
            self._scene_closed_ticks = 0

    @objc.python_method
    def _latch_exit(self):
        """A probable Mission Control exit (a click passing through, or the
        active space changing): hide everything now and keep it hidden while
        the layout stays frozen at this signature, up to _EXIT_LATCH_MAX.
        This covers the input-to-animation gap the scene probe cannot see —
        the zoom only starts a beat AFTER the click — and hands over to the
        scene gate once the transform actually begins."""
        self._exit_latched = True
        self._exit_latch_sig = self._prev_layout
        self._exit_latch_expires = time.time() + _EXIT_LATCH_MAX
        self._hide_all(immediate=True)

    @objc.python_method
    def rebuild_style(self):
        """Drop every pooled panel/button/tray/menu so the next Mission Control
        session lazily recreates them under the current Liquid Glass setting
        (read at view creation). Called from the menu-bar toggle; Mission
        Control cannot be open while its menu is, so the pools are idle."""
        self._hide_all(immediate=True)
        for panel in self._panels + self._mark_panels + self._tray_panels:
            panel.close()  # releasedWhenClosed is False; close just detaches
        self._panels = []
        self._buttons = []
        self._mark_panels = []
        self._tray_panels = []
        self._fade_target = {}
        if self._menu_panel is not None:
            self._menu_panel.close()
            self._menu_panel = None
            self._menu_view = None

    @objc.python_method
    def _hide_all(self, immediate=False):
        """Hide every overlay. `immediate` skips the fade and orders the panels
        out NOW — used whenever Mission Control is (probably) going away, since
        a fade at that point just floats buttons over the restored screen."""
        self._close_menu()
        for panel in self._panels + self._mark_panels + self._tray_panels:
            if immediate:
                self._fade_target[id(panel)] = 0  # cancel any pending fade-in
                panel.orderOut_(None)
            else:
                self._hide_panel(panel)
        self._targets = {}
        self._rects = {}
        self._mark_applied = {}
        self._tray_applied = {}

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
        if kind == "cancel":
            # Take back a pending window action; its buttons return next frame.
            self._cancel_deferred_action(title)
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
        """Queue a window action to run when Mission Control exits: 'close',
        'minimize', or one of the quit levels 'quit' (⌘Q), 'forcequit'
        (forceTerminate) and 'kill' (SIGKILL). The buttons vanish now and the
        thumbnail gets a dim scrim glyph for the rest of the session, so it's
        clear what will act on exit and the buttons can never reappear on a
        lingering thumbnail."""
        if not (title or "").strip():
            # No action can resolve an untitled window; queueing one could only
            # ever mis-target (title matching is how actions find windows).
            return
        # One pending action per thumbnail; a later menu pick (e.g. Force Quit
        # after a plain quit) replaces the earlier one.
        self._deferred_actions = [(a, t) for a, t in self._deferred_actions
                                  if t != title]
        self._deferred_actions.append((action, title))
        # Show a dim "marked" overlay on this thumbnail instead of its buttons.
        self._marked[title] = {"minimize": "−", "quit": "⏻",
                               "forcequit": "☠️", "kill": "💀", "snapleft": "◧",
                               "snapright": "◨", "snapmax": "■"}.get(action, "✕")
        for i, (k, t, e) in list(self._targets.items()):
            if t == title:
                self._panels[i].orderOut_(None)
                self._rects.pop(i, None)
        _geom_log("DEFER %s window=%r (applies on MC exit)" % (action, title))

    @objc.python_method
    def _cancel_deferred_action(self, title):
        """Take back a pending action on a marked tile/thumbnail and restore its
        buttons next frame, so the user can choose differently or leave it be.
        Covers every reversible case: a deferred window action
        (close/minimize/quit/…); the close queued for a full-screen app that has
        dropped back to the desktop (that one already left full screen — cancel
        just keeps it open as an ordinary window); and a held-reference
        full-screen close still sitting fullscreen in the Spaces Bar."""
        cancelled = False
        # Window actions queued via _defer_window_action.
        kept = []
        for action, t in self._deferred_actions:
            if t == title or ax.titles_related(title, t):
                self._marked.pop(t, None)
                cancelled = True
            else:
                kept.append((action, t))
        self._deferred_actions = kept
        # A full-screen app that un-fullscreened and is queued to close on exit.
        app = self._unfs_pending.pop(title, None)
        if app is not None:
            if app in self._deferred_app_closes:
                self._deferred_app_closes.remove(app)
            if self._pending_unfs_app == app:  # stop scrimming its reappearance
                self._pending_unfs_baseline = None
                self._pending_unfs_app = None
            cancelled = True
        # A held-reference full-screen close still in the Spaces Bar.
        if title in self._deferred_close:
            self._deferred_close.pop(title, None)
            cancelled = True
        self._marked.pop(title, None)
        # Drop the cancel button now; _sync restores the tile's own buttons on
        # the next frame, once the mark is gone.
        for i, (k, t, e) in list(self._targets.items()):
            if k == "cancel" and t == title:
                self._panels[i].orderOut_(None)
                self._rects.pop(i, None)
        if cancelled:
            _geom_log("CANCEL pending action for %r" % title)

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
            elif action == "forcequit":
                ok = self._quit_app_for(title, infos, level="force")
            elif action == "kill":
                ok = self._quit_app_for(title, infos, level="kill")
            elif action in ("snapleft", "snapright", "snapmax"):
                # Tile to a region of the window's own screen: left/right half,
                # or 'max' (fill the whole desktop). Queue it for the staged
                # processor (processArrangeQueue) rather than acting inline —
                # native tiling needs the app frontmost, and two tiles applied
                # back to back collided and mis-placed. Applied there, spaced
                # apart, native-first with a raw-resize fallback.
                region = {"snapleft": "left", "snapright": "right",
                          "snapmax": "max"}[action]
                self._arrange_queue.append((region, title))
                ok = True  # deferred to processArrangeQueue
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
        if self._arrange_queue and self._arrange_current is None:
            self.performSelector_withObject_afterDelay_(
                "processArrangeQueue", None, 0.05)

    def processArrangeQueue(self):
        """Apply the next queued tile: focus its window now, then press the
        native tile menu a beat later (pressArrangeStep) once the app has come
        forward. One window at a time so their tile animations don't collide."""
        if self._arrange_current is not None:
            return  # a tile is mid-flight; its step will chain the next
        if not self._arrange_queue:
            return
        region, title = self._arrange_queue.pop(0)
        infos = windows.list_windows(exclude_pid=os.getpid())
        pids = list({i.pid for i in infos})
        pid = ax.focus_window_by_title(title, pids)
        self._arrange_current = (title, pids, pid, region)
        # Activation is async; give the app a moment to come frontmost so its
        # tile menu item is live and targets this window.
        self.performSelector_withObject_afterDelay_("pressArrangeStep", None, 0.35)

    def pressArrangeStep(self):
        """Press the native tile menu for the focused window (raw-resize
        fallback), then schedule the next queued tile with a gap so the first
        tile settles before we switch apps."""
        current, self._arrange_current = self._arrange_current, None
        if current is not None:
            title, pids, pid, region = current
            ok = ax.tile_focused_app(pid, region) if pid else False
            if not ok:
                ok = ax.snap_window_by_title(title, pids, region)
            _geom_log("ARRANGE %s %r -> %s" % (region, title, ok))
        if self._arrange_queue:
            self.performSelector_withObject_afterDelay_(
                "processArrangeQueue", None, 0.5)

    @objc.python_method
    def _quit_app_for(self, title, infos, level="graceful"):
        """Quit the app owning the thumbnail titled `title`, by pid (so it works
        regardless of AX buttons). `level` picks how hard: 'graceful' is ⌘Q
        (terminate, may prompt to save), 'force' is Force Quit (forceTerminate,
        no prompt), 'kill' is a raw SIGKILL to the process."""
        info = self._match_window_info(title, infos)
        if info is None:
            return False
        if level == "force":
            return ax.force_quit_pid(info.pid)
        if level == "kill":
            return ax.kill_pid(info.pid)
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
            self._pending_unfs_app = title  # so a ↺ on the reappearance can undo
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
