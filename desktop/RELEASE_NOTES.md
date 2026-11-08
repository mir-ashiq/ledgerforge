# LedgerForge 0.28.0 — distribution hardening

**Phase 10 of the [v1.0 roadmap](https://github.com/mir-ashiq/ledgerforge/blob/main/ROADMAP.md):
the trust story of a download (ADR-031).** Every release now ships on
three platforms from one pipeline, every update the app ever installs is
verified against a key pinned inside the app itself before a byte of it
is used, and crash diagnostics stay on your machine unless you choose
otherwise — no telemetry by default, as a pledge, not a setting.

## What's new for the user

- **Updates that verify themselves.** On each start the app fetches the
  release's `update_manifest.json` and its detached Ed25519 signature,
  and checks it against the release key pinned in the app's own bytes —
  a manifest that does not verify is not parsed, not compared, never
  used (fail closed). A newer release downloads quietly — a
  **file-level delta** where one exists for your version, so only
  changed files move — is verified byte-for-byte against the signed
  manifest, and **applies the next time you start the app**, never while
  you work. The status bar says what happened; `Help > Check for
  Updates…` (Ctrl+U) re-runs the check; the About box shows the pinned
  key's fingerprint (`C189:C1B9:CBCB:3C73`). A power cut mid-apply
  repairs itself on the following boot. `LF_UPDATE_CHECKS=0` disables
  automatic checks; `LF_UPDATE_URL` points the app at your own mirror.
- **macOS and Linux builds, same app.** `LedgerForge-<version>-macos-arm64.zip`
  (double-click the `.app`) and `LedgerForge-<version>-linux-x64.tar.gz`
  (extract, run `LedgerForge/LedgerForge`; a `.desktop` file rides
  along) — built from the same spec as Windows, smoke-gated the same
  way, published in the same release. macOS is arm64 (Apple silicon);
  Intel Macs run from source. Ubuntu 22.04 builds the Linux package for
  glibc reach.
- **Crash reports that stay yours.** If anything crashes, a plain-JSON
  report lands in `<data>/crashes` — readable, yours, kept. **Nothing is
  ever sent anywhere** unless your administrator configured a collector
  (`LF_CRASH_ENDPOINT`) and you choose to send (`Help > Crash reports…`,
  or the explicit `LF_CRASH_REPORTING=1` opt-in). The payload carries
  version, platform and the traceback — never your books. No collector
  ships in the download; no telemetry exists to enable.
- **Code signing, armed.** The release workflow signs the Windows exe
  with Azure Trusted Signing and the macOS app with a Developer ID +
  notarization the moment the repository carries those credentials —
  this release's artifacts are **unsigned** (the project holds no
  certificate yet): SHA-256 checksums are published as `checksums.txt`,
  and the update path is Ed25519-verified regardless. The release notes
  will say "signed" the day it is true, plainly.

## What's new underneath

- **`ledgerforge/updating.py`** — the update contract: canonical-form
  signing (formatting is not the contract, content is), the pinned key,
  plan choice (delta preferred, full as the fallback), staging with
  strict set-equality against `delta.json` (zip-slip refused, every
  staged file SHA-256-verified before `pending.json` exists), and an
  idempotent apply-on-relaunch with an apply journal — the updater's
  failure mode is "no update", never "broken app".
- **`nativeapp/updater.py` + `nativeapp/crashes.py`** — the window's
  quiet halves: the background controller (QThreadPool, the ADR-022
  discipline) and the capture-always/opt-in-send reporter with its
  allowlisted payload. The engine child dumps fatal signals beside the
  GUI's reports.
- **`release-native.yml`** replaces `release-windows.yml`: a
  windows/macos/linux build matrix, per-artifact offscreen smoke gates,
  the signing gates (env-flag pattern — GitHub refuses `secrets` in
  `if:`), and a publish job that builds **file-level deltas against the
  previous release** (`scripts/make_delta.py`), writes and signs
  `update_manifest.json` (`scripts/make_update_manifest.py`, the
  `LF_UPDATE_SIGNING_KEY` Actions secret), and publishes the GitHub
  Release with checksums, deltas, manifest and signature.
- **The smoke gate grows two proofs**: the update gate (an unsigned and
  a tampered manifest are both refused; nothing staged) and the crash
  gate (a report lands, readable, allowlisted) — proven on the shipped
  bytes of every platform.
- 42 new tests (`test_updating`, `test_crashes`, `test_native_updater`)
  — **678 green**, plus the frontend suites and the E2E flow unchanged.

## The plan, on the record

The capability ledger's Deployment row flips to **parity** — landed in
v0.28.0 (Phase 10, pays the ADR-021 debt). What remains is **Phase 11 —
the parity release** itself: the accountant's day as a scripted
acceptance run in CI, performance certification at 10× the workload
model, the accessibility audit, and the v1.0 announcement.

---
