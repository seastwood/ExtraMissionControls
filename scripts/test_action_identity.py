#!/usr/bin/env python3
"""Headless identity and completion regressions. Never posts input."""
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extra_mission_controls import ax
from extra_mission_controls.mission_control import MissionControlButtons

class IdentityTests(unittest.TestCase):
    def test_capture_and_ambiguous_titles(self):
        first, second = object(), object()
        with patch.object(ax, 'window_refs', {12: [('Doc', first, 1), ('Doc', second, 2)]}), patch.object(ax, 'pid_for_title', return_value=12), patch.object(ax, 'pinned', {}):
            self.assertIsNone(ax.capture_window('Doc'))
            self.assertIsNone(ax.capture_window('Doc' + ax.KEY_SEP + '1'))
            ax.pinned['Doc' + ax.KEY_SEP + '1'] = (12, 2)
            self.assertEqual(ax.capture_window('Doc' + ax.KEY_SEP + '1'), (12, second, 2))

    def test_retained_identity_survives_retitle(self):
        window, button = object(), object()
        with patch.object(ax, 'ax_window_number', return_value=7), patch.object(ax, '_attribute', return_value=button), patch.object(ax, '_foreground_window') as front, patch.object(ax, 'press_element', return_value=True) as press, patch.object(ax, '_find_window_by_title', side_effect=AssertionError('must not search again')):
            self.assertTrue(ax.perform_captured_action((12, window, 7), 'close'))
            front.assert_called_once_with(window)
            press.assert_called_once_with(button)

    def test_replaced_identity_is_not_pressed(self):
        with patch.object(ax, 'ax_window_number', return_value=8), patch.object(ax, 'press_element') as press:
            self.assertFalse(ax.perform_captured_action((12, object(), 7), 'close'))
            press.assert_not_called()

    def test_completion_requires_observation(self):
        target = (12, object(), 7)
        with patch.object(ax.AX, 'AXUIElementCopyAttributeValue', return_value=(0, 'AXWindow')):
            self.assertFalse(ax.captured_action_state(target, 'close'))
        with patch.object(ax.AX, 'AXUIElementCopyAttributeValue', return_value=(ax.AX.kAXErrorInvalidUIElement, None)):
            self.assertTrue(ax.captured_action_state(target, 'close'))
            self.assertIsNone(ax.captured_action_state(target, 'minimize'))
        with patch.object(ax.AX, 'AXUIElementCopyAttributeValue', return_value=(0, True)):
            self.assertTrue(ax.captured_action_state(target, 'minimize'))

    def test_cancel_drops_retained_target(self):
        c = MissionControlButtons.alloc().init()
        c._deferred_actions = [('close', 'Doc')]
        c._action_targets['Doc'] = (12, object(), 7)
        c._action_started['Doc'] = 1
        c._deferred_retries['Doc'] = 1
        c._cancel_deferred_action('Doc')
        self.assertFalse(c._action_targets or c._action_started or c._deferred_retries or c._deferred_actions)

    def test_capability_filter_reflows_buttons(self):
        from scripts.test_exit_latch import make_controller, frame, thumb
        with patch.object(ax, 'action_caps_for', return_value=(False, False)):
            c = make_controller()
            frame(c, [thumb()])
            targets, _ = frame(c, [thumb()])
            self.assertNotIn('window', [k for k, _ in targets])
            self.assertNotIn('minimize', [k for k, _ in targets])
            self.assertIn('quit', [k for k, _ in targets])

if __name__ == '__main__':
    unittest.main()
