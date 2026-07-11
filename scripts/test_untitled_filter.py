#!/usr/bin/env python3
"""Unit test for the untitled-thumbnail filter: a tooltip/popup window caught
on screen as Mission Control opens (e.g. an IDE hover tip) gets its own
thumbnail with NO title — it must receive no buttons, and no deferred action
may ever be queued under an empty title.

Headless: no Mission Control, no screen, no mouse.

    python3 scripts/test_untitled_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extra_mission_controls import ax
from extra_mission_controls.mission_control import MissionControlButtons


def thumb(title):
    return {"title": title, "x": 100, "y": 100, "width": 300, "height": 200}


def main():
    c = MissionControlButtons.alloc().init()
    failures = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    # The predicate the window loop uses to skip tooltip/popup thumbnails.
    check("empty title is untitled", c._untitled(thumb("")))
    check("whitespace title is untitled", c._untitled(thumb("  ")))
    check("missing title key is untitled", c._untitled({"x": 0}))
    check("real title is not untitled", not c._untitled(thumb("Songs")))

    # No deferred action may be queued under an empty/whitespace title —
    # the traffic-light fallback matches windows by title, and an empty
    # title could mis-match some other untitled window.
    c._defer_window_action("close", "")
    c._defer_window_action("quit", "   ")
    check("empty-title actions are refused",
          c._deferred_actions == [] and c._marked == {})
    c._defer_window_action("close", "Songs")
    check("titled action still queues",
          c._deferred_actions == [("close", "Songs")]
          and c._marked.get("Songs") == "✕")

    # titles_related must never let an empty title relate to a real one
    # (this is what keeps empty-title lookups from prefix-matching the world).
    check("'' does not relate to a real title",
          not ax.titles_related("", "Songs")
          and not ax.titles_related("Songs", ""))

    # And the by-title window actions refuse empty titles outright.
    check("close_window_by_title('') is a no-op",
          ax.close_window_by_title("", []) is False)

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
