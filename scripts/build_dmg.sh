#!/bin/zsh
# Build ExtraMissionControls.app with py2app, then wrap it in a DMG.
# Usage: ./scripts/build_dmg.sh   (run from anywhere; uses the project venv)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "error: project venv not found — create it first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

VERSION="$("$PYTHON" -c 'from extra_mission_controls import __version__; print(__version__)')"

if [[ ! -f assets/icon.icns ]]; then
    "$PYTHON" scripts/make_icon.py
fi

rm -rf build dist
"$PYTHON" setup.py py2app

APP="dist/ExtraMissionControls.app"

# --- Code signing (stable identity → permissions persist across updates) ----
# Resolve a signing identity: explicit override, else a real Developer ID if
# one exists, else our stable self-signed identity (created on demand).
SELF_SIGNED="ExtraMissionControls Signing"
SIGN_KEYCHAIN="$HOME/Library/Keychains/emc-signing.keychain-db"
KEYCHAIN_ARG=()
if [[ -n "${EMC_SIGN_IDENTITY:-}" ]]; then
    IDENTITY="$EMC_SIGN_IDENTITY"
elif security find-identity -v -p codesigning 2>/dev/null \
        | grep -q "Developer ID Application"; then
    IDENTITY="$(security find-identity -v -p codesigning \
        | grep "Developer ID Application" | head -1 \
        | sed -E 's/.*"(.*)"/\1/')"
    echo "Signing with Developer ID: $IDENTITY"
else
    IDENTITY="$SELF_SIGNED"
    if ! security find-identity -p codesigning "$SIGN_KEYCHAIN" 2>/dev/null \
            | grep -qF "$SELF_SIGNED"; then
        "$PROJECT_DIR/scripts/setup_signing.sh"
    fi
    "$PROJECT_DIR/scripts/setup_signing.sh" >/dev/null  # unlock + authorize
    KEYCHAIN_ARG=(--keychain "$SIGN_KEYCHAIN")
    echo "Signing with self-signed identity: $IDENTITY"
fi

codesign --force --deep --sign "$IDENTITY" "${KEYCHAIN_ARG[@]}" \
    --identifier com.seth.extramissioncontrols "$APP"
codesign --verify --deep --strict "$APP" \
    && echo "Signature verified." \
    || { echo "error: signature verification failed" >&2; exit 1; }

STAGING="dist/dmg"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

cp assets/icon.icns "$STAGING/.VolumeIcon.icns"

# The volume custom-icon flag only sticks when set on a mounted RW image,
# so build RW, flag it, then convert to the final compressed DMG.
DMG="dist/ExtraMissionControls-$VERSION.dmg"
RW_DMG="dist/rw-tmp.dmg"
rm -f "$RW_DMG"
hdiutil create -volname "ExtraMissionControls" -srcfolder "$STAGING" \
    -ov -format UDRW "$RW_DMG"
MOUNT_DIR="$(hdiutil attach "$RW_DMG" -nobrowse | tail -1 | awk -F'\t' '{print $NF}')"
xattr -wx com.apple.FinderInfo \
    "0000000000000000040000000000000000000000000000000000000000000000" \
    "$MOUNT_DIR"
hdiutil detach "$MOUNT_DIR" -quiet
rm -f "$DMG"
hdiutil convert "$RW_DMG" -format UDZO -o "$DMG" -quiet
rm -f "$RW_DMG"

echo
echo "Built: $DMG"
