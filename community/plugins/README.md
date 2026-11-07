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
