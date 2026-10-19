#!/usr/bin/env python3
"""Headless synthetic fullscreen regressions; no events reach the desktop."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extra_mission_controls import ax, mission_control as mc


class FullscreenClickTests(unittest.TestCase):
    def setUp(self):
        self.c = mc.MissionControlButtons.alloc().init()
        self.c._active = True
        self.c._last_spaces = [dict(title='Example', x=100, y=100, height=90)]
        self.c._native_pointer_at_target = lambda job: True
        self.scheduled, self.posted = [], []
        self.c.performSelector_withObject_afterDelay_ = lambda *a: self.scheduled.append(a)
        patches = [
            patch.object(mc.Quartz, 'CGEventPost', side_effect=lambda tap, e: self.posted.append(e)),
            patch.object(mc.Quartz, 'CGEventPostToPid', side_effect=AssertionError('No process-local shortcut')),
            patch.object(ax.AX, 'AXUIElementSetAttributeValue', side_effect=AssertionError('No fullscreen AX writes')),
            patch.object(ax, 'focus_window', side_effect=AssertionError('No focusing')),
            patch.object(ax, 'mission_control_spaces', side_effect=lambda g: self.c._last_spaces),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def kinds(self):
        return [mc.Quartz.CGEventGetType(e) for e in self.posted]

    def test_hover_click_and_restore(self):
        self.assertTrue(self.c._native_drop('Example', None, False))
        self.assertEqual(self.kinds(), [mc.Quartz.kCGEventMouseMoved])
        self.assertEqual(self.scheduled[-1], ('nativeDropClick', None, mc._NATIVE_REVEAL_DELAY))
        self.c.nativeDropClick()
        self.assertEqual(self.kinds()[-4:], [mc.Quartz.kCGEventMouseMoved,
            mc.Quartz.kCGEventLeftMouseDown, mc.Quartz.kCGEventLeftMouseUp,
            mc.Quartz.kCGEventMouseMoved])
        for e in self.posted:
            self.assertEqual(mc.Quartz.CGEventGetIntegerValueField(
                e, mc.Quartz.kCGEventSourceUserData), mc._SYNTHETIC_MOUSE_TAG)

    def test_real_release_not_confused_with_synthetic(self):
        self.c._native_drop('Example', None, False)
        self.c._swallow_mouse_up = True
        self.c.nativeDropClick()
        self.assertNotIn(mc.Quartz.kCGEventLeftMouseDown, self.kinds())
        up = mc.Quartz.CGEventCreateMouseEvent(None, mc.Quartz.kCGEventLeftMouseUp,
            mc.Quartz.CGPointMake(800, 500), mc.Quartz.kCGMouseButtonLeft)
        self.assertIsNone(self.c._tap_callback(None, mc.Quartz.kCGEventLeftMouseUp, up, None))
        self.assertFalse(self.c._swallow_mouse_up)
        self.assertIs(self.c._tap_callback(None, mc.Quartz.kCGEventLeftMouseUp, up, None), up)

    def test_reflow_requires_another_hover(self):
        self.c._native_drop('Example', None, False)
        self.c._last_spaces[0]['x'] = 160
        self.c.nativeDropClick()
        self.assertNotIn(mc.Quartz.kCGEventLeftMouseDown, self.kinds())
        self.assertEqual(self.c._native_job['point'], (162, 102))
        self.c.nativeDropClick()
        down = next(e for e in self.posted if mc.Quartz.CGEventGetType(e) == mc.Quartz.kCGEventLeftMouseDown)
        self.assertEqual(mc.Quartz.CGEventGetLocation(down).x, 162)

    def test_user_moves_away_or_exits_cancels(self):
        for exited in (False, True):
            self.posted.clear()
            self.c._native_drop('Example', None, True)
            if exited:
                self.c._native_job['user_exit'] = True
            else:
                self.c._native_pointer_at_target = lambda job: False
            self.c.nativeDropClick()
            self.assertIsNone(self.c._native_job)
            self.assertIsNone(self.c._pending_unfs_baseline)
            self.assertNotIn(mc.Quartz.kCGEventLeftMouseDown, self.kinds())

    def test_two_failed_attempts_restore_controls(self):
        self.c._native_drop('Example', None, True)
        self.c.nativeDropVerify()
        self.assertEqual(self.c._native_job['attempt'], 2)
        self.c.nativeDropVerify()
        self.assertIsNone(self.c._native_job)
        self.assertEqual(self.c._fs_busy_until, 0)
        self.assertNotIn(('acted', 'Example'), self.c._recently_closed)
        self.assertFalse(self.c._deferred_actions)
        self.assertFalse(any(a[0] == 'reopenMissionControl' for a in self.scheduled))

    def test_no_overlapping_jobs(self):
        self.c._native_drop('Example', None, False)
        self.assertFalse(self.c._native_drop('Example', None, True))
        self.assertFalse(self.c._native_job['close'])

    def test_both_tile_controls_route_to_synthetic(self):
        class Panel:
            def orderOut_(self, sender):
                pass
        self.c._panels = [Panel()]
        calls = []
        self.c._native_drop = lambda title, element, close: calls.append(close)
        with patch.object(mc, '_NATIVE_FS_DROP', True):
            self.c._unfullscreen_tile(0, 'Example', object())
            self.c._close_fullscreen(0, 'Example', object())
        self.assertEqual(calls, [False, True])


if __name__ == '__main__':
    unittest.main()
