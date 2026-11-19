# Swift port: test before release

CI only proves the code compiles and that the headless logic matches the
Python tests. Nothing below has been checked on a real Mac yet. Run all of it
on macOS 27, and the macOS 26 section if you still support 26.

## Setup

1. Quit the Python app.
2. `cd swift && ./scripts/build_app.sh`, then copy `dist/Extra Mission Controls.app`
   to `/Applications` (replacing the Python build).
3. Launch it. Confirm Accessibility is still granted (System Settings → Privacy &
   Security → Accessibility). If it asks again, the signing identity didn't
   match; note that and grant it.
4. For the W / M / Q shortcuts, check Input Monitoring if macOS prompts.
5. For diagnosis, run with logs:
   `EMC_DEBUG=1 "/Applications/Extra Mission Controls.app/Contents/MacOS/ExtraMissionControls"`
   and read `~/Library/Logs/ExtraMissionControls/`.

## Menu bar

- [ ] Tray icon shows, tinted correctly in light and dark menu bars
- [ ] Menu shows "Extra Mission Controls 0.11.0"
- [ ] Liquid Glass toggle: buttons switch style the next time Mission Control opens
- [ ] Colors toggle: traffic-light hues on/off
- [ ] Open at Login: turn on, log out and in, app starts once (not twice); turn off works
- [ ] Restart relaunches a single copy
- [ ] Quit quits
- [ ] Launching a second copy replaces the first (no duplicate tray icons)

## Detection and placement

- [ ] Open Mission Control with the gesture, ⌃↑, F3 and a hot corner: buttons appear quickly on every thumbnail
- [ ] A slow, paused three-finger drag shows no buttons until the gesture commits
- [ ] A quick flick shows buttons as soon as the overview settles (no ~1.5s wait)
- [ ] Exiting (click a thumbnail, click background, ⌃↑, swipe down) leaves no buttons floating over the desktop
- [ ] Hovering thumbnails (the lift effect) doesn't make buttons flicker
- [ ] Spaces Bar previews aren't black and don't show copies of the buttons
- [ ] Multiple displays / Sidecar: buttons stay on their own display's windows
- [ ] Opening Mission Control from a full-screen app: no stray buttons on the zoom
- [ ] Empty desktop: no buttons; fullscreen previews have no EMC controls
- [ ] Sleep and wake (lid closed for a while): still works afterwards
- [ ] Sitting in Mission Control: CPU stays low (`scripts/diag_cpu.py` or Activity Monitor), compare with the Python app

## Buttons (use disposable windows)

- [ ] ✕ close: dims with the ✕ scrim, closes on exit, no ghost thumbnail
- [ ] − minimize: same, minimizes on exit
- [ ] ⤢ full screen: leaves Mission Control and the window goes full screen; hidden for windows that can't (System Settings)
- [ ] ◫ arrange: click and hover open the menu; Left / Right / Maximize tile on exit; Maximize hidden for fixed-size windows
- [ ] ⏻ quit: hover or right-click opens Quit / Force Quit / Force Kill; each works on exit
- [ ] ↺ cancel restores the buttons, and picking a different action replaces the first
- [ ] Unsaved document: the save prompt appears and isn't dismissed or repeated
- [ ] Two windows with the same title (two Terminal windows): only the clicked one acts
- [ ] Rename a window after queuing: the original window still acts
- [ ] W / M / Q over a thumbnail queue the action; pressing again takes it back
- [ ] Middle-click a thumbnail closes it
- [ ] Show Desktop button (bottom right): leaves Mission Control and sweeps windows away
- [ ] AX-opaque app (Steam or a game): close and minimize still work via the fallback

## Dock overlay (in Mission Control)

- [ ] Hovering a running app's Dock icon opens Close All Windows / Quit / Force Quit above its name
- [ ] Sliding along the Dock moves the bubble to the next icon
- [ ] A queued Dock action dims the icon and that app's thumbnails, ↺ undoes it, and it applies on exit

## macOS 26 only: Spaces Bar tiles

- [ ] Full-screen app tile ✕: drops to the desktop and closes on exit
- [ ] ❏ exits full screen without leaving Mission Control
- [ ] Split View ✕2 closes both apps

## Release

- [ ] Update notifier: with an older version number, the red dot and "Update Available" appear; Ignore hides them
- [ ] Bump `EMCVersion.fallback` in `Sources/EMCCore/Log.swift` (the build script reads it)
- [ ] Decide when the Swift app replaces the Python one on `main` and in the README
