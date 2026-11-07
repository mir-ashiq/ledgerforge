# ADR-016: The trust bridge — a signed community plugin index and a trust model for drop-ins

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Supersedes:** —

## Context

The plugin runtime (ADR-003) has two loading channels: entry-points
(first-party packs and pip installs) and a drop-in directory on
`LF_PLUGIN_PATH` — "just put the folder there", the Odoo-style
convenience. The analysis (§5.6) was honest about what that convenience
means: **anything on the drop-in path executed, no questions asked**, and
the planned mitigation — "a community plugin index (curated list + CI
badge requirements)" plus a trust model — was left as a roadmap item
(§9: "signed plugin index, capability-scoped registry, sandboxed UI
slots").

That item is now the last buildable roadmap line, and the v0.11 pack
reports made it urgent in a new way: a pack report is *served* by plugin
code, a document view *renders* money, a transformer *runs inside the
posting transaction*. The two-tier table (plugin guide) already answers
*where* untrusted code may run — the after-commit event tier. What was
missing is the answer to *who vouched for the code that runs in-process*.

What "trust" cannot mean here: sandboxing. An in-process Python plugin
with a transformer hook is, by design, trusted code — no signature scheme
changes that, the same way an apt repository's signatures do not make
packages memory-safe. What it *can* mean, and what this ADR builds:

1. **Provenance.** A drop-in loads only if someone vouched for exactly
   those bytes: a publisher whose key the operator approved (signature),
   or the operator themself (acknowledgement).
2. **Refusal before execution.** The gate reads and verifies the file
   *before the first line of plugin code runs* — an unsigned drop-in is
   not "loaded then ignored", it never executes.
3. **Visibility.** Refusals are first-class facts (`manager.refusals`,
   `GET /api/v1/plugins`, the plugins screen), because a refused plugin
   someone noticed is the model working, and one nobody noticed is not.

A latent bug made the urgency practical: drop-in discovery imported
modules *by name* (`importlib.import_module`) without ever putting the
drop-in directory on `sys.path` — the documented Odoo-style path could
not work as written. Fixing it meant touching the discovery loop anyway;
this is the loop's redesign.

## Decision

### 1. Three channels, three trust bases

| Channel | How it loads | Trust basis | Label |
|---|---|---|---|
| First-party pack (`ledgerforge.contrib.*`) | entry-point | in this repository, reviewed here | `trusted` |
| pip-installed pack | entry-point | the operator chose to install it | `trusted` |
| Drop-in (`LF_PLUGIN_PATH`) | file scan + gate | proof (below) | `signed` / `acknowledged`, else refused |

Entry-points remain trusted by *installation* — the operator's pip
channel is their own decision, the way installing any software is. The
gate is for the channel where code arrives without that decision:
files on disk.

### 2. A signature covers release facts, not just bytes

An author publishes `pack.py` plus a detached `pack.py.sig` — a JSON
document (scheme `ed25519`) whose signature covers the canonical payload
`{name, version, api_compat, sha256}`:

- **`sha256`** binds the signature to the exact artifact bytes;
- **`name`/`version`/`api_compat`** bind it to the release identity — a
  signature cannot be relabelled onto another version, another name, or
  an artifact declaring a different API floor.

The publisher's public key travels inside the document (minisign-style
self-describing signature); approval of that key is a separate, local
decision. Keys are Ed25519 via `cryptography` (already a dependency
through `pyjwt[crypto]`) — no new runtime requirements.

### 3. The gate runs before execution, in three checks

For each `*.py` on the drop-in path, `PluginManager.discover`:

1. **Acknowledgement?** `.trust.json` in the drop-in directory pins
   `module → sha256`. Exact match → load as *acknowledged*. Any byte
   change voids the pin (the plugin is refused until re-acknowledged).
2. **Signature?** Verify `module.py.sig` against the file bytes
   (mathematics), the publisher against the trust store's approved keys
   (policy), and the *statically extracted* manifest facts — an AST read
   of `PLUGIN = PluginManifest(name=…, version=…, api_compat=…)`,
   no execution — against the signed facts. All three → import, then one
   final cross-check that the executed manifest still equals the signed
   facts (a module that re-assigns `PLUGIN` after a readable first
   assignment is refused). Load as *signed*.
3. Otherwise: **refuse** — record `(module, reason)` in `refusals`, log
   loudly, never import.

The AST pre-read is why a *computed* manifest (not statically readable)
cannot load on a signature: there are no facts to pin before execution.
Such a plugin loads only through acknowledgement — the operator's eyes
replace the missing statics.

Duplicate manifest names are refused across channels: a pack installed
twice (entry-point and drop-in) would run its transformers twice on one
posting; the second registration is refused with that reason.

### 4. The community index: a curated, signed catalog

`community/plugins/index.json` — in this repository, extended by pull
request, the curation promised in §5.6. Each entry carries the release
facts (`name`, `version`, `api_compat`), the artifact reference and its
`sha256`, the `publisher_key`, and the release `signature`. The sample
entry (`webhook-forwarder`, the after-commit event tier in miniature —
the pattern an e-invoicing or BI bridge follows) is signed by the
curator's key exactly the way a third-party release would be; the
private half of that key never lives in this repository.

`ledgerforge plugins install <name>` fetches an entry's artifact (local
index or URL), verifies sha256 → signature → static facts → API floor,
then lands three files in the drop-in directory: the plugin, its `.sig`,
and the publisher's approval in `.trust.json`. Install is the only path
that *adds* a publisher key — and it adds exactly the key that signed
bytes the curator vouched for.

### 5. The CLI: one command for the whole lifecycle

```
ledgerforge plugins keygen    # an author's publisher identity
ledgerforge plugins sign      # sign one release (facts + bytes)
ledgerforge plugins verify    # offline check of a signature
ledgerforge plugins index     # list the community index
ledgerforge plugins install   # verify + land + approve (from index or URL)
ledgerforge plugins ack       # pin a reviewed local plugin's exact bytes
ledgerforge plugins list      # what would load, and why / why not
```

`ledgerforge plugins list` is the dry run of the gate: same checks, same
verdicts, no boot required.

### 6. The visibility half

`GET /api/v1/plugins` (admin) returns the loaded plugins with channel and
trust basis plus every refusal and its reason. The web plugins screen
(`Alt+X` — X for extensions, the VS Code mnemonic; palette-searchable,
`Esc` back, like every screen) shows both tables and explains the three
trust labels in place. Installing and signing stay CLI acts — they are
operator/author decisions, not bookkeeping.

## What this is not

- **Not a sandbox.** Approved publisher code runs in-process, inside
  your transactions, as before — the engine still re-validates the three
  financial laws after every hook (a plugin can enrich or veto a
  posting; it can never corrupt one), but confidentiality and CPU are
  the operator's review's job. Untrusted work belongs to the
  after-commit tier; that boundary is D3 and predates this ADR.
- **Not a key discovery protocol.** The index's publisher keys arrive
  through pull-request review, not a transparency log. If the ecosystem
  outgrows that, sigstore-style signing is the upgrade path — the
  signature *document* is the seam that would swap.
- **Not drop-in packages.** The drop-in channel is single-file modules
  (that is what `install` produces and what the gate reads); multi-file
  packs go through pip and entry-points, where packaging metadata and
  the operator's installer already carry the provenance story.

## Consequences

- **(+)** An unsigned, tampered, relabelled or future-API drop-in never
  loads — and never executes a single line of code.
- **(+)** The drop-in channel now actually works (spec-from-file-location
  imports; the directory is never on `sys.path`, so a plugin cannot
  shadow a real package), and is tested for the first time.
- **(+)** Refusals are data: the API, the CLI and the screen all answer
  "what was refused and why" — the operator sees the gate working.
- **(+)** Zero schema change, zero plugin-API change (stays 0.6): trust
  is orthogonal to the registry contract; every existing plugin loads
  unchanged.
- **(−)** The acknowledgement step is a one-time tax for existing
  drop-in users (there were none — the path did not work) and for
  computed-manifest plugins (rare by construction; the guide shows the
  static form).
- **(−)** The curator's key is a single point of failure for the sample
  index; rotation means re-signing entries and a PR — acceptable at
  index format 1, revisited if the index grows.

Zero schema change. Runtime: `plugins/trust.py` (signing, verification,
trust store, static facts), `plugins/__init__.py` (the gate, spec
imports, refusals, `LoadedPlugin`), `__main__.py` (the CLI, console
script `ledgerforge`), `api/plugins.py` (`GET /plugins`, admin).
Community: `community/plugins/index.json` + `webhook_forwarder.py` and
its `.sig` (sample entry). Web: `components/PluginsScreen.tsx` (`Alt+X`),
`api/client.ts`, `App.tsx`. Exercised by `tests/test_plugin_trust.py`
(25 tests: keygen→sign→verify, tamper/relabel/stranger-key refusal,
execution-before-gate, ack pinning and voiding, duplicates, future API,
index install paths, the committed sample entry, the CLI, the API).
