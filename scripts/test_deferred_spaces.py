#!/usr/bin/env python3
"""Deferred actions survive returning from Mission Control to another Space."""
import os
import sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extra_mission_controls import ax, windows
from extra_mission_controls.mission_control import MissionControlButtons

c = MissionControlButtons.alloc().init()
c._deferred_actions = [('close', 'Desktop document')]
c._press_held_window = lambda *args: False
scheduled = []
c.performSelector_withObject_afterDelay_ = lambda *args: scheduled.append(args)
info = windows.WindowInfo(1, 42, 'Editor', 'Desktop document', 0, 0, 800, 600)
with patch.object(ax, 'mission_control_group', return_value=None), \
     patch.object(windows, 'list_windows', return_value=[info]) as listing, \
     patch.object(ax, 'close_window_by_title', side_effect=[False, True]) as close, \
     patch.object(ax, 'pid_for_title', return_value=42), \
     patch.object(ax, 'activate_pid') as activate:
    c.applyDeferredActions()
    assert c._deferred_actions == [('close', 'Desktop document')]
    activate.assert_called_once_with(42)
    assert not c._tl_queue
    c.applyDeferredActions()
    assert not c._deferred_actions and not c._deferred_retries
    assert all(call.kwargs.get('all_spaces') for call in listing.call_args_list)
    assert close.call_args.args == ('Desktop document', [42])
print('Deferred cross-Space action regression passed')

# The foreground path must raise and act on one resolved window, in order.
window, button = object(), object()
events = []
with patch.object(ax, '_find_window_by_title', return_value=window) as find, \
     patch.object(ax, '_attribute', return_value=button), \
     patch.object(ax, '_foreground_window', side_effect=lambda w: events.append(('raise', w))), \
     patch.object(ax.AX, 'AXUIElementPerformAction',
                  side_effect=lambda b, a: events.append(('press', b)) or ax.AX.kAXErrorSuccess):
    assert ax.close_window_by_title('Document', [42], foreground=True)
    assert events == [('raise', window), ('press', button)]
    find.assert_called_once_with('Document', [42])
    events.clear()
    assert ax.minimize_window_by_title('Document', [42], foreground=True)
    assert events == [('raise', window), ('press', button)]
print('Foreground close/minimize ordering passed')
