#!/bin/zsh
# Build Extra Mission Controls.app (native Swift) and wrap it in a DMG.
#
#   ./scripts/build_app.sh                         # universal release build, signed, + DMG
#   EMC_NO_DMG=1 ./scripts/build_app.sh            # just the .app
#   EMC_SIGN_IDENTITY=- ./scripts/build_app.sh     # ad-hoc sign (CI test builds)
#
# Signing: EMC_SIGN_IDENTITY if set, else a "Developer ID Application" identity
# if one is installed, else the stable self-signed identity from
# ../scripts/setup_signing.sh (same as the Python build, so macOS keeps the
# Accessibility permission). EMC_KEYCHAIN picks the keychain to sign from.
#
# Developer ID builds get the hardened runtime and, with EMC_NOTARIZE=1 plus
# APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD, are notarized and stapled.
#
# Sparkle updates are switched on when a public EdDSA key is available:
# SPARKLE_PUBLIC_KEY, or the contents of Resources/sparkle_public_key.txt.
set -euo pipefail

SWIFT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$SWIFT_DIR/.." && pwd)"
cd "$SWIFT_DIR"

VERSION="$(sed -n 's/.*static let fallback = "\(.*\)".*/\1/p' Sources/EMCCore/Log.swift)"
APP_NAME="Extra Mission Controls"
DIST="$SWIFT_DIR/dist"
APP="$DIST/$APP_NAME.app"
FEED_URL="https://github.com/seastwood/ExtraMissionControls/releases/latest/download/appcast.xml"

# --- Build (universal when the toolchain can, host arch otherwise) ----------
if swift build -c release --arch arm64 --arch x86_64; then
    BIN_DIR="$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)"
else
    swift build -c release
    BIN_DIR="$(swift build -c release --show-bin-path)"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Frameworks"
cp "$BIN_DIR/ExtraMissionControls" "$APP/Contents/MacOS/ExtraMissionControls"
sed "s/__VERSION__/$VERSION/g" Resources/Info.plist > "$APP/Contents/Info.plist"
cp Resources/tray_icon.png "$APP/Contents/Resources/"
cp "$REPO_DIR/assets/icon.icns" "$APP/Contents/Resources/icon.icns"

# --- Sparkle --------------------------------------------------------------------
SPARKLE="$BIN_DIR/Sparkle.framework"
if [[ ! -d "$SPARKLE" ]]; then
    SPARKLE="$(find .build/artifacts -type d -path '*macos*' -name Sparkle.framework | head -1)"
fi
[[ -d "$SPARKLE" ]] || { echo "error: Sparkle.framework not found" >&2; exit 1; }
ditto "$SPARKLE" "$APP/Contents/Frameworks/Sparkle.framework"

PUBLIC_KEY="${SPARKLE_PUBLIC_KEY:-}"
if [[ -z "$PUBLIC_KEY" && -f Resources/sparkle_public_key.txt ]]; then
    PUBLIC_KEY="$(tr -d '[:space:]' < Resources/sparkle_public_key.txt)"
fi
PLIST="$APP/Contents/Info.plist"
if [[ -n "$PUBLIC_KEY" ]]; then
    plutil -insert SUPublicEDKey -string "$PUBLIC_KEY" "$PLIST"
    plutil -insert SUFeedURL -string "$FEED_URL" "$PLIST"
    plutil -insert SUEnableAutomaticChecks -bool YES "$PLIST"
    echo "Sparkle updates: on"
else
    echo "Sparkle updates: off (no public key) — the GitHub release check is used instead"
fi

# --- Code signing -------------------------------------------------------------------
SELF_SIGNED="ExtraMissionControls Signing"
SIGN_KEYCHAIN="$HOME/Library/Keychains/emc-signing.keychain-db"
KEYCHAIN_ARG=()
if [[ -n "${EMC_KEYCHAIN:-}" ]]; then KEYCHAIN_ARG=(--keychain "$EMC_KEYCHAIN"); fi
if [[ -n "${EMC_SIGN_IDENTITY:-}" ]]; then
    IDENTITY="$EMC_SIGN_IDENTITY"
elif security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
    IDENTITY="$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -1 \
        | sed -E 's/.*"(.*)"/\1/')"
else
    IDENTITY="$SELF_SIGNED"
    if ! security find-identity -p codesigning "$SIGN_KEYCHAIN" 2>/dev/null | grep -qF "$SELF_SIGNED"; then
        "$REPO_DIR/scripts/setup_signing.sh"
    fi
    "$REPO_DIR/scripts/setup_signing.sh" >/dev/null  # unlock + authorize
    KEYCHAIN_ARG=(--keychain "$SIGN_KEYCHAIN")
fi
echo "Signing with: $IDENTITY"

SIGN_OPTS=(--force --sign "$IDENTITY" "${KEYCHAIN_ARG[@]}")
DEVELOPER_ID=0
if [[ "$IDENTITY" == *"Developer ID Application"* ]]; then
    DEVELOPER_ID=1
    SIGN_OPTS+=(--options runtime --timestamp)  # required for notarization
fi

# Inside out: Sparkle's helpers, the framework, then the app.
FW="$APP/Contents/Frameworks/Sparkle.framework/Versions/B"
for nested in "$FW"/XPCServices/*.xpc(N) "$FW/Autoupdate" "$FW/Updater.app"; do
    if [[ -e "$nested" ]]; then
        codesign "${SIGN_OPTS[@]}" --preserve-metadata=entitlements "$nested"
    fi
done
codesign "${SIGN_OPTS[@]}" "$APP/Contents/Frameworks/Sparkle.framework"
codesign "${SIGN_OPTS[@]}" --identifier com.seth.extramissioncontrols "$APP"
codesign --verify --deep --strict "$APP" && echo "Signature verified."

notarize() {  # $1 = file to submit
    xcrun notarytool submit "$1" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_PASSWORD" --wait
}
NOTARIZE=0
if [[ "${EMC_NOTARIZE:-}" == 1 ]]; then
    if [[ $DEVELOPER_ID == 1 && -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
        NOTARIZE=1
        ZIP="$DIST/notarize.zip"
        ditto -c -k --keepParent "$APP" "$ZIP"
        notarize "$ZIP"
        rm -f "$ZIP"
        xcrun stapler staple "$APP"
    else
        echo "warning: EMC_NOTARIZE=1 needs a Developer ID identity and APPLE_ID/APPLE_TEAM_ID/APPLE_APP_PASSWORD; skipping" >&2
    fi
fi

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
if [[ $NOTARIZE == 1 ]]; then
    codesign --force --sign "$IDENTITY" "${KEYCHAIN_ARG[@]}" --timestamp "$DMG"
    notarize "$DMG"
    xcrun stapler staple "$DMG"
fi
echo "Built: $DMG"
