# Extra Mission Controls — native Swift version

This folder is a native Swift/AppKit port of the PyObjC app in
`extra_mission_controls/`. It is a straight translation: the same Mission
Control detection, settle machine, scene probe, deferred actions, Dock overlay
and fallbacks. The point is speed (no PyObjC bridge on every AX call or event
tap callback), a much smaller app, and normal Xcode signing.

The Python app stays on `main` as the reference until this version has passed
the checklist in [TESTING.md](TESTING.md).

## Build and run

Requires Xcode 26 or newer (Liquid Glass needs the macOS 26 SDK; older
toolchains build the flat style only).

```sh
cd swift
swift build                     # debug build
swift test                      # headless logic tests (no Mission Control needed)
swift run ExtraMissionControls  # run from the terminal (Accessibility is granted to Terminal)
EMC_DEBUG=1 swift run ExtraMissionControls   # with trace logs
open Package.swift              # or work in Xcode
```

To build the real app (signed with the same identity as the Python build, so
the Accessibility permission carries over):

```sh
./scripts/build_app.sh          # -> swift/dist/Extra Mission Controls.app + DMG
EMC_NO_DMG=1 ./scripts/build_app.sh   # just the .app
```

CI (`.github/workflows/swift.yml`) builds, tests and packages on every push to
`swift-port`. Its downloadable app is **ad-hoc signed**, so macOS treats each
CI build as a new app and asks for Accessibility again. Use the local script
for day-to-day testing.

## Layout

| File | Python origin | What it does |
| --- | --- | --- |
| `AXBasics.swift` | `ax.py` helpers, `windows.py` | AX attribute reads, CGWindowList, window enumeration |
| `PrivateAPI.swift` | `ctypes`/`loadBundleFunctions` in `ax.py` | `_AXUIElementGetWindow`, `CGS*` spaces, `CoreDockSendNotification`, resolved with `dlsym` |
| `BackdropRule.swift` | `_BackdropRule` | the one-query "is Mission Control up?" rule (learned on macOS 27) |
| `AXBridge.swift` | `ax.py` | the Mission Control snapshot, window matching, actions |
| `MissionControl*.swift` | `mission_control.py` | the controller: detection, sync, targets, input, panels, actions |
| `UI.swift` | `ui.py` | buttons, Liquid Glass, scrims, menus |
| `App.swift` | `app.py` | menu-bar shell |
| `Login.swift` | `login.py` | Open at Login |
| `Updates.swift` | `updates.py` | GitHub release check |

## Differences from the Python app

- **Open at Login** uses `SMAppService` (listed under System Settings → General
  → Login Items) instead of a LaunchAgent plist. An existing Python-era
  LaunchAgent is removed and replaced on first launch.
- **Debug logs** (`EMC_DEBUG=1`) go to `~/Library/Logs/ExtraMissionControls/`
  (`emc_trace.log`, `emc_actions.log`, `emc_diag.txt`), since a `.app` has no
  source folder. Errors still go to `~/Library/Logs/ExtraMissionControls.log`.
- **Single instance**: launching stops any other copy with the same bundle id
  (including the Python `.app`) or a `main.py` run from source.
- Version starts at **0.11.0**.
