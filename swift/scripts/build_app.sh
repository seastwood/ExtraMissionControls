#!/bin/zsh
# Build Extra Mission Controls.app (native Swift) and wrap it in a DMG.
#
#   ./scripts/build_app.sh            # universal release build, signed, + DMG
#   EMC_SIGN_IDENTITY=- ./scripts/build_app.sh   # ad-hoc sign (CI)
#   EMC_NO_DMG=1 ./scripts/build_app.sh          # just the .app
#
# Signing uses the SAME identity as the Python build (scripts/setup_signing.sh
# at the repo root) and the same bundle id, so macOS keeps the Accessibility
# permission across the switch from the Python app to this one.
set -euo pipefail

SWIFT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$SWIFT_DIR/.." && pwd)"
cd "$SWIFT_DIR"

VERSION="$(sed -n 's/.*static let fallback = "\(.*\)".*/\1/p' Sources/EMCCore/Log.swift)"
APP_NAME="Extra Mission Controls"
DIST="$SWIFT_DIR/dist"
APP="$DIST/$APP_NAME.app"

# Universal when the toolchain can do it (Xcode); host arch otherwise.
if swift build -c release --arch arm64 --arch x86_64 >/dev/null 2>&1; then
    BIN="$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)/ExtraMissionControls"
else
    swift build -c release
    BIN="$(swift build -c release --show-bin-path)/ExtraMissionControls"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/ExtraMissionControls"
sed "s/__VERSION__/$VERSION/g" Resources/Info.plist > "$APP/Contents/Info.plist"
cp Resources/tray_icon.png "$APP/Contents/Resources/"
cp "$REPO_DIR/assets/icon.icns" "$APP/Contents/Resources/icon.icns"

# --- Code signing (stable identity → permissions persist across updates) ----
SELF_SIGNED="ExtraMissionControls Signing"
SIGN_KEYCHAIN="$HOME/Library/Keychains/emc-signing.keychain-db"
KEYCHAIN_ARG=()
if [[ -n "${EMC_SIGN_IDENTITY:-}" ]]; then
    IDENTITY="$EMC_SIGN_IDENTITY"
elif security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
    IDENTITY="$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -1 \
        | sed -E 's/.*"(.*)"/\1/')"
    echo "Signing with Developer ID: $IDENTITY"
else
    IDENTITY="$SELF_SIGNED"
    if ! security find-identity -p codesigning "$SIGN_KEYCHAIN" 2>/dev/null | grep -qF "$SELF_SIGNED"; then
        "$REPO_DIR/scripts/setup_signing.sh"
    fi
    "$REPO_DIR/scripts/setup_signing.sh" >/dev/null  # unlock + authorize
    KEYCHAIN_ARG=(--keychain "$SIGN_KEYCHAIN")
    echo "Signing with self-signed identity: $IDENTITY"
fi

codesign --force --deep --sign "$IDENTITY" "${KEYCHAIN_ARG[@]}" \
    --identifier com.seth.extramissioncontrols "$APP"
codesign --verify --deep --strict "$APP" && echo "Signature verified."

echo "Built: $APP ($VERSION)"
[[ -n "${EMC_NO_DMG:-}" ]] && exit 0

STAGING="$DIST/dmg"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
cp "$REPO_DIR/assets/icon.icns" "$STAGING/.VolumeIcon.icns"

# The volume custom-icon flag only sticks on a mounted RW image.
DMG="$DIST/$APP_NAME $VERSION.dmg"
RW_DMG="$DIST/rw-tmp.dmg"
rm -f "$RW_DMG" "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDRW "$RW_DMG"
MOUNT_DIR="$(hdiutil attach "$RW_DMG" -nobrowse | tail -1 | awk -F'\t' '{print $NF}')"
xattr -wx com.apple.FinderInfo \
    "0000000000000000040000000000000000000000000000000000000000000000" "$MOUNT_DIR"
hdiutil detach "$MOUNT_DIR" -quiet
hdiutil convert "$RW_DMG" -format UDZO -o "$DMG" -quiet
rm -f "$RW_DMG"
rm -rf "$STAGING"
echo "Built: $DMG"
