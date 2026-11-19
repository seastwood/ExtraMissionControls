# Extra Mission Controls — Swift app

The native Swift/AppKit version of Extra Mission Controls. It began as a
straight port of the PyObjC app (same detection, settle machine, scene probe,
deferred actions, Dock overlay and fallbacks) and replaces it.

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
./scripts/build_app.sh                 # -> swift/dist/Extra Mission Controls.app + DMG
EMC_NO_DMG=1 ./scripts/build_app.sh    # just the .app
```

CI (`.github/workflows/swift.yml`) builds, tests and packages on every push.
Its downloadable app is **ad-hoc signed**, so macOS treats each CI build as a
new app and asks for Accessibility again. Use the local script for testing.

## Releases

Push a tag that matches `EMCVersion.fallback` in `Sources/EMCCore/Log.swift`:

```sh
git tag v0.11.0 && git push origin v0.11.0
```

`.github/workflows/release.yml` then tests, builds, signs (and notarizes, if
set up) and attaches the DMG, a zip and the Sparkle appcast to a **draft**
release. Nothing is public until you test that build and press **Publish**.

One-time setup, under the repo's **Settings → Secrets and variables → Actions**:

| Secret | What | Needed |
| --- | --- | --- |
| `EMC_SIGNING_P12` | `base64 -i <identity>.p12` — your Developer ID Application export, or the self-signed backup `~/.config/extramissioncontrols/emc-signing.p12` | yes |
| `EMC_SIGNING_P12_PASSWORD` | the .p12 password (`emc` for the self-signed backup) | yes |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | Apple ID, team ID, and an app-specific password, for notarization | Developer ID only |
| `SPARKLE_PRIVATE_KEY` | Sparkle's EdDSA private key (below) | for in-app updates |

Use the **same identity every release**: macOS ties the Accessibility
permission to it, and Sparkle refuses an update signed by a different one.
Switching from the self-signed identity to Developer ID asks every user to
grant Accessibility once more.

### In-app updates (Sparkle)

1. `swift build` once, then generate a key pair (the private key goes into your
   login keychain):
   ```sh
   "$(find .build/artifacts -name generate_keys -type f | head -1)"
   ```
2. Paste the printed public key into `Resources/sparkle_public_key.txt` and commit it.
3. Export the private key and store it as the `SPARKLE_PRIVATE_KEY` secret:
   ```sh
   "$(find .build/artifacts -name generate_keys -type f | head -1)" -x sparkle_private.key
   ```
   Then delete `sparkle_private.key`.

Builds with a public key run Sparkle (menu: **Check for Updates…**, automatic
checks) against `releases/latest/download/appcast.xml`. Builds without one fall
back to the old "Update Available" red dot. Users on the Python version get
that red dot, then install the Swift app once by hand.

## Layout

| File | What it does |
| --- | --- |
| `AXBasics.swift` | AX attribute reads, CGWindowList, window enumeration |
| `PrivateAPI.swift` | `_AXUIElementGetWindow`, `CGS*` spaces, `CoreDockSendNotification`, resolved with `dlsym` |
| `BackdropRule.swift` | the one-query "is Mission Control up?" rule (learned on macOS 27) |
| `MissionControlObserver.swift` | Dock/WindowManager AX notifications that speed up detection |
| `AXBridge.swift` | the Mission Control snapshot, window matching, actions |
| `MissionControl*.swift` | the controller: detection, sync, targets, input, panels, actions |
| `UI.swift` | buttons, Liquid Glass, scrims, menus |
| `Settings.swift`, `SettingsWindow.swift` | preferences and the Settings window |
| `App.swift` | menu-bar shell, Sparkle |
| `CrashGuard.swift` | relaunch after a crash (max 3 in 2 minutes) |
| `Login.swift` | Open at Login (`SMAppService`) |
| `Updates.swift` | GitHub release check (when Sparkle is off) |

## Differences from the Python app

- **Open at Login** uses `SMAppService` (System Settings → General → Login
  Items). A Python-era LaunchAgent is replaced on first launch.
- **Settings window**: hide buttons, change their size, rebind W / M / Q, turn
  off the Dock menu or middle-click close.
- **Crash recovery**: relaunches after a crash; **Show Crash Reports…** in the
  menu reveals macOS's report.
- **Detection**: Dock/WindowManager accessibility notifications trigger an
  immediate check. After they've announced three openings in a row the idle
  poll slows from 0.15s to 0.5s; one opening they miss restores 0.15s.
- **Hover** work is skipped while the cursor is still.
- **Arrange** on non-English systems finds the tile menu items by their
  keyboard shortcuts when the English titles don't match.
- **Debug logs** (`EMC_DEBUG=1`) go to `~/Library/Logs/ExtraMissionControls/`.
- **Single instance**: launching stops any other copy (including the Python app).
