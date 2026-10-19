#!/usr/bin/env python3
"""Test one cursor-free fullscreen exit while the user holds Mission Control open.

Run from an Accessibility-trusted terminal, visit the chosen fullscreen app,
then open Mission Control manually. No input is posted, no app is activated,
and no windows are closed. Exactly one AX request is made. The JSON report
distinguishes request acceptance, actual fullscreen state, and MC visibility.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extra_mission_controls import ax


def read_fullscreen(window):
    error, value = ax.AX.AXUIElementCopyAttributeValue(window, "AXFullScreen", None)
    return {"error": int(error), "value": bool(value) if error == 0 else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, help="Exact running app name, e.g. Notes")
    parser.add_argument("--title", help="Exact AX window title when the app has multiple fullscreen windows")
    parser.add_argument("--method", choices=("attribute", "button"), default="attribute")
    parser.add_argument("--output", default="/tmp/emc_fullscreen_api.json")
    args = parser.parse_args()
    if not ax.is_trusted():
        parser.error("Run from the same Accessibility-trusted Terminal used for main.py.")
    pids = ax._pids_named(args.app)
    if len(pids) != 1:
        parser.error("Expected exactly one running app with that name.")
    print("Visit %s's fullscreen window, then open Mission Control.\n"
          "Keep Mission Control open and don't click for five seconds.\n"
          "Waiting up to 60 seconds; Ctrl-C cancels before the request." % args.app,
          flush=True)
    window = None
    stable_since = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        group = ax.mission_control_group()
        if group is None:
            # Cache while the app exposes its windows. Never guess among twins.
            candidates = [w for w in ax._ax_windows(pids[0])
                          if ax._attribute(w, "AXFullScreen")
                          and (args.title is None or
                               ax._attribute(w, "AXTitle") == args.title)]
            if len(candidates) > 1:
                parser.error("Multiple fullscreen windows: select one with --title.")
            window = candidates[0] if candidates else None
            stable_since = None
        elif window is not None:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 1.0:
                break
        time.sleep(0.1)
    else:
        parser.error("No cached target followed by an open Mission Control. No request sent.")

    report = {"app": args.app, "pid": pids[0], "method": args.method,
              "title": str(ax._attribute(window, "AXTitle") or ""),
              "before": read_fullscreen(window), "samples": []}
    if report["before"]["value"] is not True:
        parser.error("Cached window is not verifiably fullscreen. No request sent.")
    # Check again immediately before mutation, since the user may have exited.
    if ax.mission_control_group() is None:
        parser.error("Mission Control closed before the request. No request sent.")
    started = time.monotonic()
    if args.method == "attribute":
        error, settable = ax.AX.AXUIElementIsAttributeSettable(window, "AXFullScreen", None)
        report["settable"] = {"error": int(error), "value": bool(settable)}
        report["request_error"] = int(ax.AX.AXUIElementSetAttributeValue(
            window, "AXFullScreen", False))
    else:
        button = ax._attribute(window, "AXFullScreenButton")
        if button is None:
            parser.error("Window exposes no fullscreen button. No request sent.")
        report["button_subrole"] = str(ax._attribute(button, "AXSubrole") or "")
        # AXZoomButton can mean maximize rather than fullscreen on macOS 27.
        if report["button_subrole"] != "AXFullScreenButton":
            parser.error("Button is not unambiguously a fullscreen control. No request sent.")
        report["request_error"] = int(ax.AX.AXUIElementPerformAction(button, "AXPress"))

    # Save acceptance immediately so an interrupted observation still has evidence.
    output = Path(args.output).expanduser()
    output.write_text(json.dumps(report, indent=2) + "\n")
    while time.monotonic() - started < 5.0:
        report["samples"].append({
            "elapsed": round(time.monotonic() - started, 3),
            "fullscreen": read_fullscreen(window),
            "mc_ax_present": ax.mission_control_group() is not None,
        })
        time.sleep(0.1)
    samples = report["samples"]
    report["summary"] = {
        "request_accepted": report["request_error"] == 0,
        "window_confirmed_windowed": bool(samples) and samples[-1]["fullscreen"]["value"] is False,
        "mc_present_at_every_sample": bool(samples) and all(s["mc_ax_present"] for s in samples),
        "note": "AX observations sampled at about 10 Hz; visually confirm that Mission Control stayed open.",
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2), flush=True)
    print("Report: %s" % output, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nObservation stopped. Any already-sent AX request is not undone.")
        raise SystemExit(130)
