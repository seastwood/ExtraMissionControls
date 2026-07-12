#!/bin/zsh
# Create (once) a STABLE self-signed code-signing identity for
# ExtraMissionControls, so macOS keeps granted permissions across app updates.
#
# macOS TCC keys permissions to an app's code-signing "designated requirement".
# An unsigned / ad-hoc app's requirement is the binary's cdhash, which changes
# every rebuild — so every update looks like a new app and permissions reset.
# Signing every build with the same certificate gives a stable requirement
# (identifier + certificate leaf), so permissions persist.
#
# This is idempotent: run it once. build_dmg.sh calls it automatically if the
# identity is missing. Safe to re-run.
set -euo pipefail

CERT_NAME="ExtraMissionControls Signing"
KEYCHAIN="$HOME/Library/Keychains/emc-signing.keychain-db"
# Local, non-secret password: this keychain only holds a self-signed dev cert
# whose sole purpose is reproducible signing. It guards nothing sensitive.
KC_PASS="emc-signing"
# Backup of the identity OUTSIDE the repo, so the SAME identity is restored
# even if the keychain (or the whole repo) is deleted — losing it would reset
# every user's permissions on the next update.
BACKUP_DIR="$HOME/.config/extramissioncontrols"
BACKUP_P12="$BACKUP_DIR/emc-signing.p12"
P12_PASS="emc"

have_identity() {
    # No -v: a self-signed cert is untrusted, but trust is not needed to sign
    # (nor for TCC persistence). We only need the cert + private key present.
    security find-identity -p codesigning "$KEYCHAIN" 2>/dev/null \
        | grep -qF "$CERT_NAME"
}

ensure_keychain_ready() {
    if [[ ! -f "$KEYCHAIN" ]]; then
        security create-keychain -p "$KC_PASS" "$KEYCHAIN"
    fi
    # Unlock FIRST: set-keychain-settings on a locked keychain falls back to
    # a GUI password prompt; unlock-keychain -p is non-interactive.
    security unlock-keychain -p "$KC_PASS" "$KEYCHAIN"
    security set-keychain-settings "$KEYCHAIN"          # no auto-lock timeout
    # Add to the user search list (once) so codesign can find the identity.
    if ! security list-keychains -d user | grep -qF "$KEYCHAIN"; then
        local existing
        existing=$(security list-keychains -d user | sed 's/[":]//g' | xargs)
        # shellcheck disable=SC2086
        security list-keychains -d user -s "$KEYCHAIN" ${=existing}
    fi
}

authorize_codesign() {
    # Let codesign use the private key without a GUI prompt.
    security set-key-partition-list \
        -S apple-tool:,apple:,codesign: -s -k "$KC_PASS" "$KEYCHAIN" >/dev/null
}

if have_identity; then
    echo "Signing identity already present: $CERT_NAME"
    ensure_keychain_ready
    authorize_codesign
    exit 0
fi

ensure_keychain_ready

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ -f "$BACKUP_P12" ]]; then
    echo "Restoring signing identity from backup: $BACKUP_P12"
    cp "$BACKUP_P12" "$TMP/identity.p12"
else
    echo "Generating new self-signed code-signing certificate..."
    cat > "$TMP/openssl.cnf" <<'CNF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = ExtraMissionControls Signing
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
        -config "$TMP/openssl.cnf" >/dev/null 2>&1
    openssl pkcs12 -export -out "$TMP/identity.p12" \
        -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
        -name "$CERT_NAME" -passout "pass:$P12_PASS" >/dev/null 2>&1
    mkdir -p "$BACKUP_DIR"
    cp "$TMP/identity.p12" "$BACKUP_P12"
    chmod 600 "$BACKUP_P12"
    echo "Backed up identity to: $BACKUP_P12 (keep this safe)"
fi

security import "$TMP/identity.p12" -k "$KEYCHAIN" -P "$P12_PASS" \
    -T /usr/bin/codesign -T /usr/bin/security >/dev/null
authorize_codesign

if have_identity; then
    echo "Signing identity ready: $CERT_NAME"
else
    echo "error: identity import did not produce a usable code-signing identity" >&2
    exit 1
fi
