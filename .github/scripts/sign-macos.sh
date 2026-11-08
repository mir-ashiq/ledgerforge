#!/usr/bin/env bash
# Codesign + notarize the macOS .app (ADR-031) — called by release-native.yml
# ONLY when the repository carries the signing secrets. Input: the .app path.
#
# Environment (all supplied by the workflow from secrets):
#   MACOS_SIGNING_CERT_P12  – the signing certificate, base64-encoded PKCS#12
#   MACOS_SIGNING_PASSWORD  – the .p12 import password
#   MACOS_SIGNING_IDENTITY  – e.g. "Developer ID Application: Name (TEAMID)"
#   APPLE_ID / APPLE_PASSWORD / APPLE_TEAM_ID – the notarytool credentials
set -euo pipefail

APP="$1"
[ -n "${MACOS_SIGNING_CERT_P12:-}" ] || { echo "no certificate configured — skipping"; exit 0; }
[ -n "${MACOS_SIGNING_IDENTITY:-}" ] || { echo "ERROR: MACOS_SIGNING_IDENTITY missing"; exit 1; }

KEYCHAIN=build.keychain-db
PASSWORD=build-$(date +%s)

security create-keychain -p "$PASSWORD" "$KEYCHAIN"
security default-keychain -s "$KEYCHAIN"
security unlock-keychain -p "$PASSWORD" "$KEYCHAIN"
echo "$MACOS_SIGNING_CERT_P12" | base64 --decode > cert.p12
security import cert.p12 -k "$KEYCHAIN" -P "$MACOS_SIGNING_PASSWORD" -T /usr/bin/codesign
shred -u cert.p12 || rm -f cert.p12
security set-key-partition-list -S apple-tool:,apple: -k "$PASSWORD" "$KEYCHAIN"

# hardened runtime, timestamped — the notary service's requirements
codesign --force --deep --options runtime --timestamp \
  --sign "$MACOS_SIGNING_IDENTITY" "$APP"
codesign --verify --strict --verbose=2 "$APP"

if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_PASSWORD:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ]; then
  ditto -c -k --keepParent "$APP" notary.zip
  xcrun notarytool submit notary.zip --apple-id "$APPLE_ID" \
    --password "$APPLE_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait || {
      echo "::warning::notarization submission failed — the app is signed but not notarized"
    }
  xcrun stapler staple "$APP" || echo "::warning::stapling failed"
  rm -f notary.zip
fi

security default-keychain -s login.keychain-db
security delete-keychain "$KEYCHAIN"
echo "signed: $APP"
