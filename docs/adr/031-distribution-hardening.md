# ADR-031: Distribution hardening — signed releases, three platforms, updates that verify themselves, crashes that stay yours

- **Status:** Accepted (2026-11-09)
- **Deciders:** mir-ashiq
- **Supersedes:** —
- **Pays the debt of:** ADR-021 (the "unsigned binary" consequence — signing,
  auto-update and cross-platform packaging were its recorded follow-ups),
  ADR-022 (the native shell those releases ship), ADR-016 (the Ed25519
  sign-before-trust discipline, applied to the application itself)

## Context

ROADMAP.md §3 Phase 10 (v0.28.0, issue #16): Windows code signing, macOS and
Linux native packages of the same Qt shell, auto-update (signed manifest,
delta-aware downloader, update-on-relaunch), and opt-in crash reporting with
no telemetry by default. v0.18–v0.27 closed every *capability* gap against
TallyPrime; what remains before v1.0 is distribution itself. Four facts
shaped the design.

**First — a downloaded zip is a trust decision, and ours is anonymous.** ADR-021
recorded the honest consequence of no certificate: SmartScreen warns, and the
mitigation was checksums the user has to know to check. The fix has two
halves: *code signing* where a certificate exists (Azure Trusted Signing in
the release workflow, armed by repository secrets the maintainer adds), and
*self-verifying updates* where it doesn't — the update path must not depend
on the user manually comparing hex strings.

**Second — an auto-updater is an attack surface with a GUI.** The industry's
worst incidents (supply-chain via update channels) all share one shape: the
client fetched something and executed it because the fetch succeeded. The
countermeasure is already in this repository: ADR-016 verifies Ed25519
signatures *before* a plugin module is ever imported. The updater applies
the same law to the application itself: a manifest is fetched, and **nothing
at all happens — not even a version comparison — until its detached
signature verifies against a public key pinned in the shipped binary.** The
private half lives only as a GitHub Actions secret; releases sign
themselves in the pipeline. Every artifact the updater would install
carries a SHA-256 in that signed manifest, and the install aborts on any
mismatch.

**Third — an onedir bundle makes binary deltas a poor trade.** PyInstaller's
folder is thousands of files whose majority don't change between releases;
binary diffing (bsdiff/xdelta) would add native dependencies and CPU cost to
save bytes that a file-level pass already saves. The delta format is
therefore **file-level**: `changed`/`added` files with their SHA-256s plus a
`removed` list, packed as a zip. The client downloads the delta for its
current version when one exists (a typical release moves a fraction of the
bytes), falls back to the full zip otherwise, and **verifies every staged
file's hash before writing a byte of the install directory**. Apply happens
on the next relaunch — never while the engine child or the window runs.

**Fourth — "no telemetry by default" is a pledge, not a setting.** A crash
reporter that phones home unless disabled is telemetry with better manners.
LedgerForge inverts it: crashes are *always* captured — locally, in the
user's own data directory, as plain JSON they can read — and *sending*
anywhere requires the operator to configure an endpoint AND choose to send.
The payload is an allowlist (version, platform, traceback, tail of the
engine log), never book data; the pledge is written into the About box, the
README and this ADR, and the default distribution collects nothing, sends
nothing, and contains no collector to send to.

## Decision

### 1. The release pipeline becomes one workflow, three platforms

`release-native.yml` replaces `release-windows.yml` (whose Windows steps it
carries verbatim): a build matrix — `windows-latest` (x64), `macos-latest`
(arm64), `ubuntu-22.04` (x64; the oldest glibc GitHub still hosts, for
binary compatibility) — each job building the **same spec**
(`desktop/ledgerforge.spec`, now platform-aware), smoking the shipped
artifact offscreen (`--smoke`, the ADR-022 gate, on every platform), and
uploading an artifact. A `publish` job that needs all three:

- **signs** the Windows exe when Azure Trusted Signing secrets are present
  (`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`/
  `TRUSTED_SIGNING_ACCOUNT_NAME`/`TRUSTED_SIGNING_PROFILE`/
  `TRUSTED_SIGNING_ENDPOINT` — the account's regional endpoint, e.g.
  `https://weu.codesigning.azure.net/`, which the v2
  `Azure/artifact-signing-action` requires; the arm flag needs tenant id
  *and* endpoint, so a half-configured repository stays honestly unsigned),
  and the macOS
  app when Apple credentials are present (`MACOS_SIGNING_IDENTITY` +
  notarytool keychain); without secrets the artifacts stay unsigned —
  checksummed, manifest-signed, honestly labeled, never silently so;
- packages `LedgerForge-<tag>-win64.zip`, `LedgerForge-<tag>-macos-arm64.zip`
  (the `.app` inside), `LedgerForge-<tag>-linux-x64.tar.gz`, each with
  SHA-256 in `checksums.txt`;
- **generates deltas** against the previous release (`scripts/make_delta.py`,
  file-level, per platform) when one exists;
- **writes and signs `update_manifest.json`** (`scripts/make_update_manifest.py`,
  Ed25519 over the canonical JSON bytes, `update_manifest.sig` beside it)
  and publishes the GitHub Release with all of it.

### 2. `ledgerforge/updating.py` — the update contract (kernel, Qt-free)

- **Manifest**: `{product, version, released_at, platforms: {windows-x64,
  macos-arm64, linux-x64: {url, sha256, size, format, deltas: {<from>:
  {url, sha256, size}}}}, notes_url}`. Canonical bytes are
  `json.dumps(sort_keys=True, separators=(",", ":"))` — the ADR-016 form;
  the signature is a detached base64 Ed25519 signature file. `PINNED_UPDATE_KEY`
  (and its fingerprint, printed in the About box) lives in the source; the
  client refuses any manifest it cannot verify — fail closed, including
  "no signature present".
- **`check(url, current_version, fetch)`** → a plan (newer version, chosen
  artifact: the delta for `current_version` if offered, else the full zip)
  or `None`. `fetch` is injectable — every network touch in this module goes
  through it — so the test suite exercises the whole protocol without a
  socket. URLs must be `https:` (or `file:` for tests); anything else is
  refused.
- **`download(plan, staging_dir, fetch)`** → fetches, verifies the artifact
  SHA-256, unpacks, verifies every inner file against the delta/full
  manifest, and writes `pending.json` — the *only* commit point. A staged
  update that fails any hash leaves `pending.json` unwritten: the next boot
  ignores the staging directory entirely.
- **`apply_pending(data_dir, install_dir)`** — the update-on-relaunch entry
  point, called by the frozen launcher *before Qt or the engine child
  exists*. Writes an apply journal, copies changed/added files over the
  install directory, unlinks removals, deletes the journal, prunes the
  staging directory. Idempotent by construction (copies and unlinks are), so
  a power cut mid-apply is repaired by the next relaunch. Only the frozen
  app self-applies; a source run gets a notice, never a self-modification.
- **Version order**: dotted-numeric with pre-release awareness
  (`1.0.0-rc.1 < 1.0.0`), `packaging`-free — the same arithmetic a
  human does, written down.

### 3. `nativeapp/updater.py` + the window — the visible half

An `UpdateController` (QThreadPool worker, signals back to the GUI thread —
the ADR-022 threading discipline) checks on boot when enabled
(`LF_UPDATE_CHECKS=0` disables; `LF_UPDATE_URL` overrides the default — the
GitHub `releases/latest/download/update_manifest.json` redirect), downloads
the staged update quietly, and the status bar says what happened:
"up to date", "update vX.Y.Z available — will apply on restart" (and the
Help menu's *Check for Updates…* re-runs it on demand). No dialogs interrupt
bookkeeping; updates never restart the app under the user's hands. The About
box gains the update state and the pinned key's fingerprint.

### 4. `nativeapp/crashes.py` — capture always local, sending always chosen

- `install_handlers(data_dir)`: `sys.excepthook` + `threading.excepthook` +
  `faulthandler` (fatal native signals dump to `<data>/crashes/fault-*.log`);
  the engine child gets the same treatment in its own process. A report is
  a structured JSON **allowlist**: app, version, platform, machine, python,
  frozen, occurred_at, kind, traceback, and the tail of `engine.log` —
  never book data, never the database, never the command line.
- `send_report(path, endpoint)`: POSTs one report **only** when the operator
  configured `LF_CRASH_ENDPOINT`; `LF_CRASH_REPORTING=1` opts a machine into
  sending new reports automatically on next launch. The Help menu's *Crash
  reports…* opens the folder — the reports are the user's files, on the
  user's disk, in plain JSON, forever.
- The distribution ships **no collector**; an endpoint is the operator's own
  (or none at all — the default).

### 5. Platform notes, stated honestly

- macOS is arm64-only in packages (pgserver and PySide6 both ship arm64
  wheels; Intel Macs run from source — recorded, not hidden). Notarization
  rides on secrets the maintainer supplies; unsigned macOS zips carry the
  same manifest signature as everything else.
- The Linux package is a tar.gz (the `.desktop` file and icon ride along),
  built on 22.04 for glibc reach; AppImage/dpkg are future work, not
  promises.
- Windows remains the primary artifact; the exe's version resource, the
  manifest, and `ledgerforge.__version__` all derive from one place.

## Consequences

- **(+)** The SmartScreen story resolves the honest way: signed when the
  project holds a certificate (the workflow is already armed for Azure
  Trusted Signing), and *even unsigned*, every update the app ever installs
  is Ed25519-verified against a key pinned in the binary — the trust path
  no longer runs through eyeballing hex strings.
- **(+)** One spec, three platforms, one publish job: releases stop being
  "the Windows zip plus hopes"; every platform ships the same smoke-gated
  artifact.
- **(+)** A delta update moves only changed files; verification happens
  before any install byte is written; a mid-apply crash repairs on the next
  relaunch. The updater's failure mode is "no update", never "broken app".
- **(+)** Crash diagnostics exist for everyone (local JSON) without a single
  packet leaving anyone's machine by default — the pledge is structural.
- **(−)** The pinned key means a compromised signing secret is a release
  compromise; rotation requires shipping a new binary with a new pinned key
  (and a key-rotation entry in the manifest is deliberately *not* first-
  class — a second key is a second attack surface; rotate by release).
- **(−)** File-level deltas regenerate on the CI runner (unzip two releases,
  hash every file) — minutes of CI, not seconds; accepted for the bandwidth
  savings and the zero-dependency format.
- **(−)** Unsigned releases persist until the community funds signing; the
  release notes will keep saying so plainly.

Shipped as: `src/ledgerforge/updating.py` (the contract),
`src/ledgerforge/nativeapp/updater.py` (the controller),
`src/ledgerforge/nativeapp/crashes.py` (capture + opt-in send),
`scripts/make_update_manifest.py` + `scripts/make_delta.py` (the pipeline
tools), `desktop/ledgerforge.spec` (tri-platform) and
`.github/workflows/release-native.yml` (the matrix + publish). Exercised by
`tests/test_updating.py` (canonical bytes, sign/verify round-trip and every
tamper case, version order, delta build/apply/idempotence, staging
verification, fail-closed fetches), `tests/test_crashes.py` (capture,
allowlist, opt-in send on/off, engine-child fault file) and
`tests/test_native_updater.py` (the controller against a file://-served
signed manifest, offscreen) — 636 + 42 new tests green.
