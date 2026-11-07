# ADR-019: Sandboxed UI slots — plugin dashboards without keyboard scope

- **Status:** Accepted (2026-11-07)
- **Deciders:** mir-ashiq
- **Relates to:** ADR-003 (plugin architecture), ADR-004 (keyboard-first
  frontend), ADR-016 (plugin trust), analysis.md §5.5 (the Phase-3 design
  this implements), §5.2 D4 and M4

## Context

§5.5 reserved the last extensibility surface for Phase 3: dashboards are
**plugin-declared slots** rendered by the host. The constraint pair that
shaped the design (M4 + D4):

- **M4** — untrusted UI contributions must be sandboxable without poisoning
  the keyboard experience.
- **D4** — the interaction model is keyboard-first: one capture-phase
  keymap, focus scopes, the command palette as the universal discoverability
  surface.

An untrusted dashboard may want arbitrary scripts, styles and network
calls; the host must give it none of its origin, credentials, DOM or
keystroke scope — while still making its actions keyboard-reachable, or it
would regress D4 for anyone who uses it.

## Decision

Plugin API **0.7** adds one manifest fact and zero registry surface:
`PluginManifest.slots` — a tuple of `SlotSpec(key, title, html | url)`.

### 1. Manifest declaration, host rendering

Slots are declared, not registered (the §5.2 design: "UI slots are
registered by manifest declaration and activated by the host"). The loader
validates each spec (slug key, one of html/url, 128 KiB inline cap) and
refuses duplicates across packs loudly, like every other name collision.
ADR-016's gate is untouched: the static facts still read
`name/version/api_compat` without executing the module, and the slot HTML
is covered by the artifact-bytes signature the curator signs.

- **Inline HTML** is served by the host at `GET /api/v1/slots/{key}`
  (reader role) with `Content-Security-Policy: sandbox allow-scripts` and
  `nosniff` — the browser treats the document as sandboxed content even if
  an embedder forgets the iframe attribute.
- **URL** slots are embedded at their own http(s) origin directly.

### 2. The iframe is opaque-origin and relay-fed

The web UI embeds every slot as `<iframe sandbox="allow-scripts">` — no
`allow-same-origin`, so the sandbox gets an **opaque origin**: no cookies,
no localStorage, no host DOM, no same-origin fetches. The slot speaks the
versioned REST API **through the host's postMessage relay** instead:

```
host → slot   lf-slot:init {slot, apiBase}                 (on iframe load)
slot → host   lf-slot:register-commands [{id, title, key?}]
host → slot   lf-slot:command {id}                         (host-bound keys)
slot → host   lf-slot:request {id, path}                   (GET, versioned API)
host → slot   lf-slot:response {id, ok, status, body}
slot → host   lf-slot:close                                (Esc inside sandbox)
```

The relay is GET-only and path-checked (versioned-API paths; no schemes,
no protocol-relative hosts); the host performs the fetch with its own
session and hands back parsed JSON. The sandbox holds no credentials and
can reach nothing the host declines to fetch. This is §5.5's "sandboxed
iframe speaking the versioned REST API" with the host as the transport —
a strictly smaller trust surface than giving the sandbox network access.

### 3. Keyboard scope stays with the host (M4 + D4)

The slot never receives keyboard scope — it cannot: it is a separate
document with an opaque origin. Instead it **registers commands**; the host
binds them (through keyboard-kit, with `when` predicates active only while
the slot is mounted) and routes keystrokes back as `lf-slot:command`.
Every slot command therefore appears in the **command palette** for free —
mouse-free discoverability for third-party functionality, exactly §5.5's
promise. If focus lands inside the sandbox, Escape still means back: the
slot asks for `lf-slot:close` and the host pops it.

### 4. The screen and the demo

`Alt+U` opens the slots screen (U for UI extensions; Alt+P/B/X were taken):
one row per declared slot with its owning pack and trust basis — visibility
parity with the plugins screen, so an operator always knows who wrote the
panel on screen. `GET /api/v1/slots` is the picker's feed.

`ledgerforge.contrib.example_slot` is the plugin guide's teaching module
(entry-point, first-party): "Balance glance" — a trial-balance dashboard
that exercises the whole contract (relay-fed data, two registered commands
with host-bound `R`/`L`, Escape-aware, dark-themed).

## What deliberately did *not* happen

- **No federated modules yet.** §5.5 sketches Module Federation with a
  shared React runtime for *trusted* sources; that requires a build-time
  contract between host and pack versions. The sandboxed slot serves the
  untrusted case completely; federation stays a future trusted-channel
  option.
- **No kernel knowledge.** The kernel does not know slots exist beyond the
  manifest field; the API layer lists and serves them, the UI renders them.
- **No auth changes.** The relay rides the host's session; with OIDC on,
  a slot sees exactly what the operator's reader role sees — nothing more.

## Consequences

- A pack can ship a dashboard in one manifest attribute; a hostile one
  gains nothing but a rectangle and a relay.
- The contract is testable end-to-end without a browser network stack
  (postMessage is pure DOM): the frontend suite drives the protocol with
  only fetch mocked; a live browser check (accessibility tree) confirms
  the palette carries slot commands.
- Plugin API 0.6 packs are unaffected (`slots` defaults to empty); a 0.6
  pack that declares slots still loads on a 0.7 host — it simply would not
  load on older hosts, which is the floor semantics' business as usual.
