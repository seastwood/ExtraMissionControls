#!/usr/bin/env python3
"""Headless regressions for clicks queued across Mission Control reflows."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extra_mission_controls.mission_control import MissionControlButtons


def main():
    c = MissionControlButtons.alloc().init()
    c._active = True
    target = ("window", "Original", None)
    c._targets = {0: target}
    c._rects = {0: (100, 100, 24, 24)}
    calls = []
    c._close_index = lambda index: calls.append(("close", index))
    c._open_menu_for = lambda index: calls.append(("menu", index))
    c._dispatch_button(0, target, False)
    c._dispatch_button(0, target, True)
    assert calls == [("close", 0), ("menu", 0)]
    calls.clear()

    c._targets[0] = ("window", "Replacement", None)
    c._dispatch_button(0, target, False)
    c._dispatch_button(0, target, True)
    assert not calls, "A queued click must never act on a replacement window"

    c._targets[0] = target
    c._rects.clear()
    c._dispatch_button(0, target, False)
    assert not calls, "Hidden controls must reject queued clicks"

    c._rects[0] = (100, 100, 24, 24)
    c._active = False
    c._dispatch_button(0, target, True)
    assert not calls, "Exited Mission Control must reject queued menus"

    c._targets.clear()
    c._deferred_actions = [("close", "Original")]
    c._marked = {"Original": "✕"}
    c._watch_sig = ((1, 100, 100, 400, 300),)
    c._cancel_deferred_action("Original")
    assert c._watch_sig is None, "Undo must bypass the quiet heartbeat"
    assert not c._deferred_actions and not c._marked
    print("all input dispatch regressions passed")


if __name__ == "__main__":
    main()
