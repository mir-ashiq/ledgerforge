# The community plugin index

A **curated** catalog of community plugins for LedgerForge — the §5.6
promise ("curated list + CI badge requirements") made concrete. Entries
land by pull request; the bar for merging is the curation policy below.

The index format (`index_version: 1`) is a JSON file where each entry
carries everything an operator's `ledgerforge plugins install` needs to
make a trust decision *without executing anything*:

| Field | Meaning |
|---|---|
| `name` / `version` / `api_compat` | the release identity (the signature covers these) |
| `summary` / `author` / `homepage` | what a human reads before installing |
| `artifact` | the release file, relative to the index |
| `sha256` | the exact bytes the curator reviewed |
| `publisher_key` | the author's Ed25519 public key (base64) |
| `signature` | the author's signature over `{name, version, api_compat, sha256}` |

The sample entry — [`webhook-forwarder`](webhook_forwarder.py) — is a
complete, signed release demonstrating the whole chain: it is the
after-commit event tier in miniature (forwards `voucher.posted` to
`LF_WEBHOOK_URL`), exactly the pattern an e-invoicing or BI bridge
follows, and the shape any community release should take.

## Curation policy (what a PR must show)

1. **A working signature.** The entry's `publisher_key` must verify the
   `signature` over the artifact's exact bytes and declared facts —
   CI pins this (`tests/test_plugin_trust.py` verifies the committed
   sample; regenerated entries are checked the same way at review).
2. **Tests in the repo.** The plugin's source repository must show a
   passing CI badge on the release commit.
3. **A declared license.** The plugin API boundary lets community packs
   carry any license their authors choose (D5); the entry must say which.
4. **The manifest is static** — `PLUGIN = PluginManifest(name=…,
   version=…, api_compat=…)` with plain string keywords, declaring the
   oldest plugin API the code runs on.
5. **No secrets, no telemetry phoning home by default.** Event-tier
   bridges must be inert until the operator configures them (the sample's
   `LF_WEBHOOK_URL` rule).
6. **One artifact per release.** A new version is a new entry (name +
   version + sha256 + signature); old releases stay listed for books
   that pin them.

## Curator key

Index entries are signed by their authors; the **sample** entry is
signed by the repository curator's key (fingerprint
`4717:BB7D:D7AB:E4F3`). The private half never lives in this
repository. Third-party entries must be signed by *their author's* key —
the index records whose key vouches for which release, so an operator's
trust store always names a real identity, not "the index".

## Curator key rotation (the runbook)

ADR-016 accepted a single curator key at index format 1 with manual
rotation; v0.16 tools the rotation so it is safe and boring. Rotation is
a **re-attestation, never a republish**: every existing signature is
verified before anything is rewritten, the `sha256` claims cannot move,
and the result must audit clean.

1. **Generate the successor key** on the curator's machine:
   `ledgerforge plugins keygen --out curator-2027` (private key never
   leaves that machine).
2. **Audit the index as it stands**:
   `ledgerforge plugins verify-index` — a rotation never starts from a
   broken index.
3. **Re-sign every entry**:
   `ledgerforge plugins rotate-curator --new-key curator-2027/publisher_ed25519.pem \
      --old-key <current-key>.pem`
   `--old-key` is the tripwire: any entry *not* signed by the retiring
   key is refused (a mixed-key index must be resolved by hand, not
   silently adopted). The command verifies each old signature first and
   rewrites nothing if any entry fails.
4. **Prove the result**: `ledgerforge plugins verify-index` again, then
   commit the re-signed `index.json` and open the PR — reviewers diff
   `publisher_key`/`signature` lines and nothing else.
5. **Announce the retirement** of the old fingerprint. Operators holding
   the old key in `.trust.json` keep their existing installs working;
   the next release of a plugin they follow asks for one re-approval
   (`ledgerforge plugins install <name>`), which lands the new key.
6. **Destroy the old private key** once the PR is merged and mirrored
   everywhere the index is published.

Third-party authors rotate their own keys the same way, entry by entry:
sign the release with the new key, publish the new `publisher_key` +
`signature` in the entry, and note the change in the PR.
