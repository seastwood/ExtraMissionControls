<p align="center">
  <img src="assets/icon_256.png" width="128" alt="ExtraMissionControls icon">
</p>

<h1 align="center">ExtraMissionControls</h1>

<p align="center">
  ✕ close buttons for the <em>real</em> macOS Mission Control —<br>
  close any window or fullscreen space right from the overview.
</p>

<p align="center">
  <a href="https://buymeacoffee.com/eastwoodsee">
    <img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support%20this%20project-FFDD00?logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee">
  </a>
</p>

---

Apple never shipped a close button in Mission Control. This menu-bar app adds
one: open Mission Control the way you always do (swipe up / ⌃↑ / F3) and every
window thumbnail — and every fullscreen app's tile in the Spaces Bar — gains
an **✕ in its top-left corner**, with hover highlighting. Click it and the
window closes while **Mission Control stays open**.

## Features

- ✕ buttons on all window thumbnails on the current space
- ✕ buttons on fullscreen apps in the Spaces Bar (top section) — closes the
  fullscreen window and its space, staying inside Mission Control
- Split View tiles get a single **✕2** button that closes both apps of the
  split (closing just one half is not reliably possible on macOS)
- Hover effect (macOS traffic-light red)
- Buttons track Mission Control's layout live (thumbnail re-flow, Spaces Bar
  expanding/shrinking)
- Closes even apps with broken accessibility support (e.g. Steam) via a
  clicking fallback
- Just a menu-bar item — no Dock icon, no windows of its own
- Near-zero idle cost: one cheap WindowServer query 4×/s (with timer
  tolerance so macOS coalesces wake-ups); window references refresh only on
  real events (launch, space switches, app activations) instead of polling,
  so no other app is ever woken while you're not using Mission Control

## How it works

Mission Control is rendered by the system `Dock` process and cannot be
modified directly, but the Dock publishes the thumbnail layout through the
Accessibility API (an `AXGroup` with identifier `mc` appears in its AX tree
while Mission Control is active, with one `AXButton` per thumbnail exposing
title/position/size). This app:

1. polls the Dock's AX tree to detect Mission Control opening, then re-syncs
   button positions at 10 Hz so they track thumbnails re-flowing and the
   Spaces Bar expanding/shrinking (✕ buttons scale down on small tiles),
2. floats a tiny always-on-top ✕ panel over each window thumbnail and each
   fullscreen app's Spaces Bar tile (never plain "Desktop N" tiles),
3. intercepts clicks on those rects with a `CGEventTap` (while Mission Control
   is up, WindowServer routes mouse events to the Dock even though our panels
   render on top — the tap claims clicks on our buttons before the Dock sees
   them, and only those clicks), and
4. closes the clicked window by pressing its real close button via the
   Accessibility API. Mission Control stays open and re-flows.

This is the same overlay technique Mission Control Plus is understood to use —
no SIP changes, no code injection.

Closing fullscreen windows needs extra machinery: apps stop reporting their
AX windows entirely while Mission Control is open, and most only report
current-space windows even normally. The app therefore keeps an accumulative
registry of fullscreen windows — scanned while Mission Control is closed and
refreshed on every space switch — whose held AX references remain pressable
from anywhere. A close press is never trusted blindly: the app verifies the
tile actually disappeared, and if the press was silently ignored (some apps
disable their close button while fullscreen) it has the Dock exit the space's
fullscreen state in place (`AXRemoveDesktop` — you stay in Mission Control)
and closes the now-normal window through a freshly resolved close button,
double-checking once Mission Control exits.

When nothing else works (an app with a hollow accessibility tree, like
Steam), the ✕ raises the window, clicks its real traffic-light close button
with a synthetic mouse click — guarded by a frontmost-window check, cursor
restored — and puts you back into Mission Control afterwards.

## Requirements

- macOS 13+ (developed on macOS 26)
- Python 3.9+ with [PyObjC](https://pyobjc.readthedocs.io/) (development)
- Or just install the DMG

## Install

**From the DMG:** build or download `ExtraMissionControls-<version>.dmg`,
drag the app to Applications, launch it, and grant the permissions below.

**From source:**

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### Running from PyCharm

1. Open this folder as a project.
2. **Settings → Project → Python Interpreter → Add Interpreter → Existing**,
   pick `.venv/bin/python`.
3. Run `main.py`.

### Permissions (important!)

Granted to **whichever app launches the Python process** — PyCharm during
development, ExtraMissionControls.app when installed from the DMG:

| Permission | Grants | Without it |
|---|---|---|
| **Accessibility** | Mission Control detection, click interception, closing windows | Nothing works |
| **Screen Recording** | Window titles in the CG window list, used to match thumbnails to windows in the fallback close paths | Apps with broken accessibility (e.g. Steam) can't be closed |

Grant both in **System Settings → Privacy & Security**, then fully restart
the launching app — permissions only apply to newly launched processes. The
app prompts for both on first launch and prints a console warning when either
is missing.

## Building the DMG

```sh
./scripts/build_dmg.sh
```

Runs py2app and produces `dist/ExtraMissionControls-<version>.dmg` with a
drag-to-Applications layout, app icon, and volume icon. The icon itself is
generated by `scripts/make_icon.py` (vector-drawn PyObjC, no image assets).

## Permissions that survive updates (code signing)

macOS ties granted permissions to an app's **code-signing identity**, not its
name or location. An unsigned / ad-hoc app's identity is a hash of its binary,
which changes on every build — so each update looks like a brand-new app and
Accessibility / Screen Recording get **reset**.

To fix this, every build is signed with a **stable** identity, so the signing
requirement (`identifier + certificate leaf`) is identical across rebuilds and
permissions persist. `build_dmg.sh` picks, in order:

1. `$EMC_SIGN_IDENTITY` if set;
2. a **Developer ID Application** certificate if you have one (best — also
   passes Gatekeeper when notarized);
3. otherwise a **self-signed** identity it creates on first build via
   `scripts/setup_signing.sh`.

The self-signed identity is generated once and backed up to
`~/.config/extramissioncontrols/emc-signing.p12` so the *same* identity is
reused even if the keychain or repo is deleted (**keep this file** — losing it
resets everyone's permissions on the next update). It lives in a dedicated
`emc-signing` keychain; nothing sensitive is stored.

**First install of the signed app:** because the identity changed from the old
ad-hoc build, grant the permissions once more (and remove any stale
ExtraMissionControls entries in System Settings → Privacy & Security first).
Every update after that keeps them.

**Gatekeeper:** a self-signed app isn't notarized, so the first launch is
blocked — right-click the app → **Open** (once). For clean distribution to
others, use a Developer ID certificate and notarize.

## Testing

Automated end-to-end tests (they briefly take over screen and mouse: opening
Mission Control, clicking ✕ buttons with synthetic events, verifying windows
really closed):

```sh
.venv/bin/python scripts/test_mc_e2e.py                    # Finder victim
EMC_VICTIM=textedit .venv/bin/python scripts/test_mc_e2e.py # held-ref path
.venv/bin/python scripts/test_mc_fullscreen_e2e.py          # Spaces Bar tile
```

Env hooks: `EMC_DEBUG=1` (log close paths), `EMC_NO_REGISTRY=1` /
`EMC_FORCE_UNFS=1` (force the fallback paths under test).

## Project layout

| File | Purpose |
|---|---|
| `main.py` | Entry point (run this in PyCharm) |
| `extra_mission_controls/app.py` | Menu-bar shell, permission prompts |
| `extra_mission_controls/mission_control.py` | ✕ buttons over Mission Control (AX polling + event tap + close chains) |
| `extra_mission_controls/windows.py` | Window enumeration (CG window list) |
| `extra_mission_controls/ax.py` | Dock AX tree reading; window close primitives |
| `extra_mission_controls/ui.py` | Shared ✕ button styling |
| `scripts/make_icon.py` | Generates the app/volume icon |
| `scripts/setup_signing.sh` | Creates the stable self-signed signing identity |
| `scripts/probe_dock_ax.py` | Dev tool: dump the Dock's AX tree with Mission Control open |
| `scripts/test_mc_e2e.py` | E2E test: window thumbnails |
| `scripts/test_mc_fullscreen_e2e.py` | E2E test: Spaces Bar fullscreen tiles |
| `setup.py`, `scripts/build_dmg.sh` | py2app + DMG packaging |

## Known limitations

- The Dock's Mission Control AX layout (`mc` / `mc.display` / `mc.windows`
  identifiers) is undocumented and can change between macOS releases — this is
  the fragile part of the technique, verified working on macOS 26.
- Windows are matched to thumbnails by title; two windows with identical
  titles in the same app may close the wrong twin.
- Buttons appear on the current display only (multi-monitor support TODO).
- Fullscreen windows the app has never had AX sight of (fullscreened before
  the app launched, space never visited since) close via a fallback that
  briefly leaves Mission Control and returns.

## Support

If this scratches an itch Apple wouldn't:
**[buy me a coffee ☕](https://buymeacoffee.com/eastwoodsee)**
