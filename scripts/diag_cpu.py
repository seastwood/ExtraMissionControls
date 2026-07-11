#!/usr/bin/env python3
"""Measure EMC's CPU while Mission Control sits open. Takes over the screen
~20s: launches main.py, opens Mission Control for ~8 seconds while sampling
the app's %CPU (ps) twice a second, then closes MC and reports idle CPU too.

    .venv/bin/python scripts/diag_cpu.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extra_mission_controls import ax  # noqa: E402


def cpu_of(pid):
    out = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def sample(pid, seconds, label):
    readings = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        value = cpu_of(pid)
        if value is not None:
            readings.append(value)
        time.sleep(0.5)
    if readings:
        print("   %s: avg %.1f%%  max %.1f%%  (%d samples)"
              % (label, sum(readings) / len(readings), max(readings),
                 len(readings)))
    return readings


def main():
    project = os.path.join(os.path.dirname(__file__), "..")
    log = open("/tmp/emc_cpu_app.log", "w")
    app = subprocess.Popen(
        [os.path.join(project, ".venv/bin/python"),
         os.path.join(project, "main.py")],
        stdout=log, stderr=subprocess.STDOUT)
    try:
        print("1. app launched (pid %d); letting it settle..." % app.pid)
        time.sleep(2.5)
        sample(app.pid, 3.0, "idle (MC closed)")

        print("2. opening Mission Control, sampling ~8s...")
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        time.sleep(1.5)  # entry animation + settle
        sample(app.pid, 8.0, "MC open, settled")

        print("3. closing Mission Control...")
        subprocess.run(["open", "-b", "com.apple.exposelauncher"], check=False)
        time.sleep(1.5)
        sample(app.pid, 3.0, "idle again")
    finally:
        app.terminate()
        log.close()


if __name__ == "__main__":
    main()
