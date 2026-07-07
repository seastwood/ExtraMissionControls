"""py2app build configuration. Build the .app + DMG with scripts/build_dmg.sh."""

from setuptools import setup

from extra_mission_controls import __version__

APP = ["main.py"]

OPTIONS = {
    "packages": ["extra_mission_controls"],
    "iconfile": "assets/icon.icns",
    "plist": {
        "CFBundleName": "ExtraMissionControls",
        "CFBundleDisplayName": "ExtraMissionControls",
        "CFBundleIdentifier": "com.seth.extramissioncontrols",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # menu-bar app: no Dock icon
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    },
}

setup(
    name="ExtraMissionControls",
    version=__version__,
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
