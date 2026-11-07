"""The UI-slot teaching module (ADR-019, plugin API 0.7).

A minimal pack whose entire contribution is one sandboxed dashboard slot —
the plugin guide's worked example for the §5.5 contract:

- the manifest **declares** the slot (``slots=(SlotSpec(...),)``) and the
  host serves it at ``GET /api/v1/slots/{key}``;
- the web UI renders it in a hardened iframe (``sandbox="allow-scripts"``,
  opaque origin — no cookies, no localStorage, no DOM access to the host);
- the slot never receives keyboard scope. It **registers commands** with
  the host over postMessage (refresh, toggle rows) and the host binds keys
  to them while the slot is open;
- the slot speaks the versioned REST API **through the host's relay**
  (``lf-slot:request`` → ``lf-slot:response``): the host performs the GET
  with its own session and hands back JSON — the sandbox holds no
  credentials and can reach nothing else;
- Escape inside the sandbox asks the host to close (``lf-slot:close``) —
  the universal back still works even when focus is trapped in the iframe.

The module registers nothing else — no transformers, no validators — so it
composes with every other pack.
"""

from __future__ import annotations

from ..plugins import PluginManifest, SlotSpec

DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Balance glance</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; font: 13px/1.45 ui-monospace, "Cascadia Mono", Menlo, monospace;
    background: #10141a; color: #d7dde6; padding: 14px 16px;
  }
  h1 { font-size: 13px; letter-spacing: .08em; text-transform: uppercase;
       color: #8fa1b8; margin: 0 0 10px; font-weight: 600; }
  .totals { display: flex; gap: 18px; margin-bottom: 12px; flex-wrap: wrap; }
  .total { border: 1px solid #232b36; border-radius: 6px; padding: 8px 12px;
           min-width: 120px; }
  .total .k { color: #8fa1b8; font-size: 11px; }
  .total .v { font-size: 15px; margin-top: 2px; }
  .ok  { color: #7ee2a8; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 3px 10px 3px 0; border-bottom: 1px solid #1a212b; }
  th { color: #8fa1b8; font-weight: 500; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #status { color: #8fa1b8; }
  footer { margin-top: 10px; color: #63718a; font-size: 11px; }
  kbd { border: 1px solid #2c3644; border-bottom-width: 2px; border-radius: 4px;
        padding: 0 5px; font-size: 11px; }
</style>
</head>
<body>
<h1>Trial balance at a glance</h1>
<div class="totals">
  <div class="total"><div class="k">Debits</div><div class="v" id="dr">–</div></div>
  <div class="total"><div class="k">Credits</div><div class="v" id="cr">–</div></div>
  <div class="total"><div class="k">Difference</div><div class="v ok" id="diff">–</div></div>
</div>
<div id="status">connecting to the host…</div>
<table id="rows" hidden><thead><tr>
  <th>Ledger</th><th>Group</th><th class="num">Debit</th><th class="num">Credit</th>
</tr></thead><tbody></tbody></table>
<footer>
  sandboxed slot · speaks the versioned REST API through the host relay ·
  commands registered with the host: <kbd>R</kbd> refresh · <kbd>L</kbd> rows ·
  <kbd>Esc</kbd> back
</footer>
<script>
"use strict";
const send = (msg) => window.parent.postMessage(msg, "*");
let apiBase = "/api/v1";
let lastBody = null;

window.addEventListener("message", (event) => {
  const m = event.data || {};
  if (m.type === "lf-slot:init") {
    apiBase = m.apiBase || apiBase;
    // M4 + D4: the slot NEVER takes keyboard scope — it registers its
    // commands with the host, and the host binds the keys while it is open.
    send({ type: "lf-slot:register-commands", commands: [
      { id: "example-slot.refresh", title: "Balance glance: refresh", key: "R" },
      { id: "example-slot.rows", title: "Balance glance: show/hide ledger rows", key: "L" },
    ]});
    document.getElementById("status").textContent = "asking the host for the trial balance…";
    send({ type: "lf-slot:request", id: "tb", path: "/reports/trial-balance?fast=true" });
  } else if (m.type === "lf-slot:response" && m.id === "tb") {
    if (!m.ok) {
      document.getElementById("status").textContent =
        "the host refused: " + (m.error || ("HTTP " + m.status));
      return;
    }
    lastBody = m.body;
    render(m.body);
  } else if (m.type === "lf-slot:command") {
    if (m.id === "example-slot.refresh" && lastBody) {
      document.getElementById("status").textContent = "refreshing…";
      send({ type: "lf-slot:request", id: "tb", path: "/reports/trial-balance?fast=true" });
    } else if (m.id === "example-slot.rows") {
      toggleRows();
    }
  }
});

// if focus lands inside the sandbox, Escape still means "back" — by asking
// the host, not by touching anything outside the iframe
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") send({ type: "lf-slot:close" });
});

function render(tb) {
  document.getElementById("status").hidden = true;
  document.getElementById("dr").textContent = tb.totals.dr;
  document.getElementById("cr").textContent = tb.totals.cr;
  document.getElementById("diff").textContent = tb.totals.difference;
  const tbody = document.querySelector("#rows tbody");
  tbody.replaceChildren();
  for (const row of tb.ledgers) {
    const tr = document.createElement("tr");
    for (const [cls, text] of [
      ["", row.ledger_name], ["", row.group_name],
      ["num", row.dr], ["num", row.cr],
    ]) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  document.getElementById("rows").hidden = false;
}

function toggleRows() {
  if (lastBody) document.getElementById("rows").hidden =
    !document.getElementById("rows").hidden;
}
</script>
</body>
</html>
"""

PLUGIN = PluginManifest(
    name="example-slot",
    version="0.1.0",
    api_compat="0.7",
    slots=(
        SlotSpec(
            key="balance-glance",
            title="Balance glance — trial balance in a sandbox",
            html=DEMO_HTML,
        ),
    ),
)


def register(registry) -> None:  # noqa: ANN001 — the plugin API's shape
    """Nothing to register: the slot is manifest-declared, host-rendered."""
