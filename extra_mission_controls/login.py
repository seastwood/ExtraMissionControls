"""Open at Login: a per-user LaunchAgent that starts EMC when you log in.

A LaunchAgent works the same whether EMC runs as the built .app or from
source, and needs no extra framework. macOS shows its usual "background item
added" notice the first time, and lists it under System Settings → General →
Login Items, where it can also be switched off.
"""

import os
import plistlib
import subprocess
import sys

LABEL = "com.seth.extramissioncontrols"
_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def _app_bundle_path():
    """The enclosing .app when running as the built app, else None."""
    if "RESOURCEPATH" in os.environ:  # set by the py2app bootstrap
        from Foundation import NSBundle
        return NSBundle.mainBundle().bundlePath()
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/"
    if marker in exe:
        return exe[:exe.index(marker) + len(".app")]
    return None


def _program_arguments():
    bundle = _app_bundle_path()
    if bundle:
        return ["/usr/bin/open", "-a", bundle]
    main_py = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")
    return [sys.executable, main_py]


def enabled():
    return os.path.exists(_PLIST)


def set_enabled(on):
    """Install or remove the LaunchAgent. Returns the new state."""
    if on:
        os.makedirs(os.path.dirname(_PLIST), exist_ok=True)
        args = _program_arguments()
        plist = {
            "Label": LABEL,
            "ProgramArguments": args,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }
        if not _app_bundle_path():
            plist["WorkingDirectory"] = os.path.dirname(args[1])
        with open(_PLIST, "wb") as fh:
            plistlib.dump(plist, fh)
        return True
    try:
        # Unload quietly in case it's loaded; then delete the file.
        subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), _PLIST],
                       capture_output=True)
        os.remove(_PLIST)
    except OSError:
        pass
    return enabled()
