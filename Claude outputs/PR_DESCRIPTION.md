# ExtraMissionControls 0.10.0 — macOS 27 support

macOS 27 moved Mission Control's accessibility tree from the Dock into WindowManager and changed how it draws and lays out overview windows. This release brings EMC fully up to date with it and adds a few features.

## macOS 27 fixes
- **Mission Control detection:** reads the tree from WindowManager (or the Dock on older systems) and learns macOS 27's backdrop windows for cheap idle polling.
- **Ghost window:** full-screen spaces' windows, which macOS 27 lists stacked in the centre of the desktop overview, no longer get buttons. Neither do other desktops' windows, which macOS 27 lists on an empty desktop.
- **Untitled windows** (e.g. Safari's Start Page) get buttons and resolve to their app.
- **Spaces Bar previews:** no more black previews. The screen-sized scene probe was replaced by two tiny corner probes, and overlay panels stay capturable.
- **Exit full screen / close from the Spaces Bar:** `AXRemoveDesktop` strands the window invisibly on macOS 27, so both tile buttons now switch to the app's space and act there. Only one full-screen action runs at a time, and none starts until Mission Control has fully closed (overlapping animations wedged WindowManager).
- **Spaces Bar button placement:** macOS 27 reports the whole tile rather than its preview; buttons are placed inside the preview again, clear of macOS's own close button.
- **Stable overlays:** buttons, trays and marks keep their own panel, so nothing slides in from another window after a click.
- **Per-window buttons:** ⤢ is hidden for windows that can't go full screen, and Maximize is hidden for fixed-size windows.
- **Quit / full screen** resolve the app from the thumbnail's bundle id rather than window titles.

## New
- **Dock bubble:** in Mission Control, hover a running app's Dock icon to Hide, Close All Windows, Quit or Force Quit it. The app name is the caption, and ↺ undoes the action.
- **Keyboard shortcuts:** W / M / Q over a thumbnail to close / minimize / quit. These run on a separate event tap.
- **Middle-click** a thumbnail to close it.
- **Open at Login** (per-user LaunchAgent).
- **Multi-display** aware on-screen, zoom and Spaces Bar checks.
- **Power-symbol glyphs** (⏻ ⏼ ⭘).
- **Defaults:** monochrome + Liquid Glass.

## Housekeeping
- **Screen Recording is no longer requested.** It was only needed to read other apps' window titles.
- **Ctrl+C works,** and a new launch replaces any older running copy.
- **Diagnostics are debug-only** (`EMC_DEBUG=1`); normal runs write nothing.
- **Removed:** dead code from superseded approaches.

## Testing
Tested by hand on macOS 27 throughout (single display). Not yet exercised: multiple displays, and the built `.app` (login item, permissions).
