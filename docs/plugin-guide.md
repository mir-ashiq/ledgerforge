# Plugin Guide

**Runtime:** `ledgerforge.plugins.PluginManager` · **Worked examples:** `ledgerforge/contrib/example_gst.py` (the teaching module — tax transform), `ledgerforge/contrib/example_slot.py` (the UI-slot teaching module — a sandboxed dashboard), `ledgerforge/contrib/gst.py` (a real localization pack: place of supply, HSN rate tables, GSTR statements, the IRN outbox), `ledgerforge/contrib/invoicing.py` (a complete document pack), `ledgerforge/contrib/inventory.py` (derived state on the document store), `ledgerforge/contrib/payroll.py` (fiscal-year folds) and `ledgerforge/contrib/statutory.py` (a pack report); **publishing:** `community/plugins/webhook_forwarder.py` (a signed community release) · **Architecture:** ADR-003, ADR-010, ADR-011, ADR-012, ADR-013, ADR-014, ADR-016, ADR-017, ADR-019, `docs/analysis.md` §5.

## The two tiers

| Tier | Runs | Trust | Use for |
|---|---|---|---|
| **In-process plugin** | inside the posting transaction | trusted code | tax transforms, posting policy validators, report contributors |
| **Event subscriber** | after commit (webhooks/queue) | untrusted-friendly | notifications, e-invoicing bridges, BI sync, caches |

## Anatomy of a plugin

A plugin is a Python module exposing a `PLUGIN` manifest and a `register(registry)` function:

```python
from decimal import Decimal
from ledgerforge.plugins import PluginManifest, ValidationError

PLUGIN = PluginManifest(name="my-gst-pack", version="0.1.0", api_compat="0.2")


def register(registry):
    registry.register_transformer(add_gst_lines)
    registry.register_validator(no_weekend_posting)
    registry.on("voucher.posted", notify_warehouse)


def add_gst_lines(draft, resolve):
    """Intra-state GST: split rate/2 into CGST + SGST, add to the party line."""
    cfg = draft.meta.get("gst")
    if not cfg:
        return draft  # pure function: no meta, no change
    rate = Decimal(str(cfg["rate"]))  # Law 3: never float
    tax = draft.lines[0].amount * rate / Decimal(100)
    ...
    return draft.with_extra_lines([...])  # engine re-validates Law 1 anyway


def no_weekend_posting(draft, resolve):
    if draft.meta.get("policy:no_weekends") and draft.voucher_date.weekday() >= 5:
        raise ValidationError("Weekend posting is blocked by policy")


def notify_warehouse(event): ...  # after commit; exceptions are logged, never break posting
```

### Hook contracts

- **Transformer** `(draft: VoucherDraft, resolve: Resolver) -> VoucherDraft` — may add/adjust lines before engine validation. Runs inside the transaction; raising aborts the whole posting.
- **Validator** `(draft, resolve) -> None` — raise `ValidationError` to veto (HTTP 422). Runs inside the transaction.
- **Event handler** `(event: DomainEvent) -> None` — receives `voucher.posted` (the full document payload) and `master.created` (`kind` of `group` or `ledger` with the master's identity and placement — fired by `POST /api/v1/groups` and `POST /api/v1/ledgers`; `Resolver.ensure_ledger` creates in-transaction and fires nothing) after commit. Failures are isolated.
- **Document-view factory** `(ctx: DocumentContext) -> DocumentView | None` — **plugin API 0.5 (ADR-013):** owns the print layout for `meta[key]` documents. Return a view to claim, `None` to decline (the voucher slip takes over). See [Printing a document](#printing-a-document-api-05-adr-013).
- **Report factory** `(resolver, params) -> PackReport` — **plugin API 0.6 (ADR-014):** publishes a book-scoped report under a key, served at `GET /api/v1/reports/pack/{key}` as PDF (through the shared layout engine) or CSV — and, with ``data`` set, as JSON under ``?format=json`` (**plugin API 0.8, ADR-023**: the structured rows a UI consumes, the same fold the PDF renders from). See [Publishing a report](#publishing-a-report-api-06-adr-014).

`Resolver` (inside-transaction helper):

- `resolve.ledger(name=…)` / `resolve.group(name)` — book-scoped master lookups.
- `resolve.ensure_ledger(name, group_name)` — look up or atomically create under a group of the same book (e.g. `CGST Payable` under *Duties & Taxes*).
- `resolve.prior_documents(key)` — **plugin API 0.3 (ADR-011):** every posted voucher of the book whose `meta` carries `key`, oldest first, as `PriorDocument(voucher_id, voucher_no, voucher_date, reversal_of, meta)` — plus every voucher reversing one of those, so folds can net document pairs. The kernel interprets nothing; the payload is exactly what was stored.
- `resolve.lock(name)` — **plugin API 0.3 (ADR-011):** transaction-scoped advisory lock on `"{book}:{name}"`, released at commit/rollback. Lock a voucher's names in **one sorted pass** (deadlock discipline).
- `resolve.fy_start_month` — **plugin API 0.4 (ADR-012):** the book's fiscal-year start month, 1–12, read-only — the one fact a year-to-date fold needs to window its accumulators. Immutable once the book has vouchers.
- `resolve.ledger_entries(ledger_id)` — **plugin API 0.8 (ADR-023):** every posted entry of the book on one ledger, oldest first, as `LedgerEntryLine(voucher_id, voucher_no, voucher_date, reversal_of, dc, amount, narration, meta)` — the engine-side rows a ledger statement folds, with the carrying voucher's `meta` so a pack can join its own documents (bill links) to the accounting lines. Book-scoped like every Resolver read; a ledger of another book answers empty.

`api_compat` is a **floor, not a pin** (ADR-011): a plugin declaring an older minor keeps loading on a newer build; a plugin declaring a newer minor is refused loudly.

## Printing a document (API 0.5, ADR-013)

Every posted voucher is printable: `GET /api/v1/vouchers/{id}/pdf` offers the voucher's meta keys, in order, to registered document-view factories; the first non-`None` view wins, and anything unclaimed — plain vouchers, reversals, payouts — prints as the **voucher slip** (the Dr/Cr table). A pack claims its document's layout with one function:

```python
from ledgerforge.plugins import (
    DocumentColumn,
    DocumentContext,
    DocumentSection,
    DocumentView,
    PluginManifest,
)
from ledgerforge.reporting.printing import format_amount

PLUGIN = PluginManifest(name="delivery-notes", version="0.1.0", api_compat="0.5")


def register(registry):
    registry.register_document_view("delivery", delivery_note_view)


def delivery_note_view(ctx: DocumentContext) -> DocumentView | None:
    doc = ctx.meta.get("delivery")
    if not isinstance(doc, dict):
        return None  # not ours — the voucher slip takes over
    return DocumentView(
        title="Delivery Note",
        subtitle=ctx.book_name,  # the layout engine places everything
        reference=ctx.voucher_no,  # top-right
        fields=(("Consignee", str(doc.get("consignee", ""))),),
        sections=(
            DocumentSection(
                title="Items",
                columns=(
                    DocumentColumn("Item", width=2.4),  # relative widths
                    DocumentColumn("Quantity", width=0.8, align="right"),
                ),
                rows=tuple((i["item"], str(i["qty"])) for i in doc.get("items", [])),
            ),
        ),
        totals=(("Total packages", str(len(doc.get("items", [])))),),
        footnotes=("Printed from immutable posted entries (Law 2).",),
        filename=f"Delivery {ctx.voucher_no}",
    )
```

The contract:

- **`DocumentContext`** — the read side: the voucher's immutable fields, its entries as flat `EntryLine`s, the book's name, the caller's query params (`ctx.params["employee"]` picks a payroll run's payslip subject), and a book-scoped `Resolver` for derived reads. Raise `ValidationError` for caller errors (a payroll run covering several employees without `?employee=` is a 422 naming them).
- **`DocumentView`** — data, not geometry: you describe *what* the document says; `reporting.printing` owns pagination, column geometry, alignment and page furniture. One visual language for every pack's documents. Format money with `format_amount` (exact `Decimal`, thousands-separated, never `-0.00`) — never float (Law 3).
- **Decline with `None`** — the payroll factory declines payouts; unregistered keys fall through the same way.
- The web app's payslip screen is the reference consumer: `Alt+P` → pick employee and period → `Ctrl+P` prints (`tests/test_printing.py` + `frontend/web/tests/payslipScreen.spec.tsx` pin the whole path).

## Creating your own voucher-type series (API 0.9, ADR-024)

A pack whose documents deserve their own numbering — the orders pack's
QT/SO/DN/PO/GRN series — creates the voucher type lazily, inside the
posting transaction:

```python
vtype = resolver.ensure_voucher_type("WO", "Work Order", "WO")
draft.voucher_type = vtype.code  # the engine resolves the type AFTER transformers
```

`ensure_voucher_type(code, name, prefix, restart_yearly=True)` is
book-scoped and idempotent (the `ensure_ledger` pattern applied to
document numbering); a rewrite of `draft.voucher_type` in a transformer
lands in the numbering because the engine resolves the type after every
transformer has run. The type rolls back with a failed post — a refused
document consumes no number. The design contract is
[ADR-024](adr/024-order-processing.md); the worked pack is
`ledgerforge/contrib/orders.py`.

## Claiming an inbound webhook (API 0.11, ADR-026)

```python
def register(registry):
    registry.register_webhook("my-bank-gateway", handle_gateway)


def handle_gateway(resolver, payload):
    # resolver.session / resolver.book are the documented handles a
    # webhook writes through; ordinary hooks (transformers, validators)
    # never need them — they already run inside the posting transaction
    receipt = import_statement(
        resolver.session,
        book=resolver.book,
        ledger=...,
        fmt=payload["format"],
        text=payload["text"],
    )
    return receipt
```

``POST /api/v1/hooks/{key}`` runs the handler with a book-scoped
`Resolver` in the request's transaction and returns its dict as the
response — book-scoped by the standard `X-Book-Id` header, **bookkeeper
role** (webhooks write). One handler per key; a later registration
replaces an earlier one, the registries' rule. The banking pack claims
`bank-statement-gateway` (the generic gateway JSON — any connected
banking bridge can POST it); a per-bank adapter translates its bank's
payload and claims its own key — the kernel never learns a bank's name
(ADR-026 D3). Bank credentials belong to the adapter or a proxy in
front of the route, never to core.

API 0.11 also adds `Resolver.bank_lines(ledger_id)` — every imported
statement line of a bank ledger with its live-match state, the banking
pack's fold input (the `fy_start_month` precedent: a domain read a
pack needs). Bank events fire after commit like `voucher.posted`:
`bank.statement.imported`, `bank.line.matched`, `bank.line.unmatched`.

## Reading the stock masters (API 0.12, ADR-027)

Phase 6 promoted inventory to depth: batches, serials, expiry, godowns,
re-order levels, price lists and BOMs. The **movements** stay documents
(`meta.stock`, ADR-011 — the pack's fold); the **masters** are kernel
schema (migration `0009_stock_depth`), and a pack reads them through four
Resolver handles — the `ledger`/`group` discipline applied to stock, so
packs never touch the session for them:

```python
master = resolver.stock_item(name="Amoxycillin 500")  # or None — items
#       stay free strings unless a master exists (depth is opted into)
if master and master.track_batches:
    ...  # goods-in must name its batch; bool flags + reorder Decimals

for master in resolver.stock_items():  # the shortfall fold
    ...

bom = resolver.stock_bom(name="Desk-STD")  # or None
bom.output_item, bom.output_qty  # "Desk", Decimal("2")
bom.components  # (("Plank", Decimal("6")), ...)

rate = resolver.stock_price("Amoxycillin 500", "MedPlus", on=voucher_date)
#       the date-effective list price; None is the honest "no suggestion"
```

The handles return frozen data objects (`StockItemMaster`,
`StockBomMaster`), book-scoped, immutable snapshots — never ORM rows. The
inventory pack is the worked example: it enforces the tracking modes on
`meta.stock` documents and validates the manufacturing journal against the
named BOM, entirely through these handles. The API floor (ADR-011) keeps
every older pack loading unchanged.

## Reading the GST portal downloads (API 0.13, ADR-028)

The GST pack's reconciliation compares the books' inward register against
a downloaded GSTR-2A/2B statement — external facts the kernel stores
(migration `0010`, the banking precedent) and serves to packs through one
versioned read:

```python
def register(registry) -> None:
    registry.register_report("gst-recon", recon_report)


def recon_report(resolver, params):
    period = params["period"]  # YYYY-MM
    kind = params.get("kind", "2b")  # or "2a"
    gstin = params.get("gstin") or None  # one registration, or all
    portal_rows = resolver.gst_portal_lines(kind, period, gstin)
    ...
```

`gst_portal_lines` returns frozen `PortalLine` snapshots — the latest
imported snapshot per registration, exact `Decimal` amounts as stored,
book-scoped like every Resolver read (another book's statement answers
empty). The kernel stays comparison-blind: it stores and folds portal
facts; the pack decides what a row means. Importing is a bookkeeper API
(`POST /api/v1/gst/portal/import`, the `lf-gst-2a/1` / `lf-gst-2b/1`
adapter contract, all-or-nothing, idempotent by fingerprint) — never a
pack concern.

## Cost allocations ride the draft (API 0.10, ADR-025)

Cost-centre splits are **core schema** (migration 0007), not a pack document
— and they ride the draft like FX does: every `DraftLine` may carry
`cost_allocations: list[DraftCostAllocation]`, and the engine re-proves the
law (Σallocation = the line's amount, exactly) after every hook:

```python
from ledgerforge.plugins import DraftCostAllocation


def transform(draft, resolver):
    # a transformer that splits a line MUST split its allocations with it
    big = next(ln for ln in draft.lines if ln.amount > 10_000)
    big.cost_allocations = [
        DraftCostAllocation(centre_name="Mumbai", amount=big.amount / 2),
        DraftCostAllocation(centre_name="Pune", amount=big.amount / 2),
    ]
    return draft
```

A line is fully allocated or left unallocated — a partial split is a clean
422 naming the line, and the database re-proves it at commit (the Law-1
discipline). `dc` is not carried on the allocation: it is inherited from the
line, preserved exactly. Packs that never touch allocations see nothing
change; the design contract is [ADR-025](adr/025-cost-centres.md).

## Publishing a report (API 0.6, ADR-014)

A document view prints *one voucher*; a report folds *many* — a month of
payroll runs, a quarter of invoices. Packs publish those under a key and
the kernel serves them at `GET /api/v1/reports/pack/{key}`:

```python
from ledgerforge.plugins import PackReport, PluginManifest

PLUGIN = PluginManifest(name="tds-pack", version="0.1.0", api_compat="0.6")
```

### Declaring a UI slot (API 0.7, ADR-019)

A pack may declare sandboxed dashboards the host renders for you — no
registry call, one manifest attribute:

```python
from ledgerforge.plugins import PluginManifest, SlotSpec

PLUGIN = PluginManifest(
    name="my-dash-pack",
    version="0.1.0",
    api_compat="0.7",
    slots=(
        SlotSpec(
            key="my-dashboard",  # lowercase slug, unique across packs
            title="My dashboard",  # shown in the Alt+U picker
            html=HTML,  # inline (<=128 KiB), or url="https://…"
        ),
    ),
)
```

The document runs in an **opaque-origin sandbox** (`sandbox="allow-scripts"`
— no cookies, no host DOM). It talks to the engine through the host's
GET-only postMessage relay (`lf-slot:request` → `lf-slot:response`) and
registers commands (`lf-slot:register-commands`) that the host binds and
surfaces in the command palette — a slot never receives keyboard scope.
Escape inside the sandbox posts `lf-slot:close`. The full protocol and a
complete dashboard live in `ledgerforge/contrib/example_slot.py`;
the design contract is [ADR-019](adr/019-sandbox-ui-slots.md).

The report example, complete with the 0.8 JSON payload:

```python
def register(registry):
    registry.register_report("tds", tds_report)


def tds_report(resolver, params) -> PackReport:
    """?month=YYYY-MM is required; anything else the pack validates itself
    (raise ValidationError for caller errors — the route maps it to 422)."""
    month = params.get("month", "")
    if not month:
        raise ValidationError("tds: pass ?month=YYYY-MM")
    rows = fold_documents(resolver.prior_documents("invoice"), month)
    data = {"month": month, "rows": [row_as_json(r) for r in rows]}  # plugin API 0.8
    if params.get("format") == "csv":
        return PackReport(
            title=f"TDS {month}", filename=f"TDS {month}", csv=to_csv(rows), data=data
        )
    return PackReport(title=f"TDS {month}", filename=f"TDS {month}", view=tds_view(rows), data=data)
```

With `data` set, `GET /api/v1/reports/pack/{key}?format=json` answers the
payload as JSON — one fold feeding the PDF, the CSV *and* a UI. The bills
pack (ADR-023) is the reference consumer-producer: its `bills-open` report
is what both allocation dialogs read.

The contract:

- **`resolver`** is book-scoped (ADR-009) — the report reads one book's
  documents; `resolver.prior_documents(key)` is the same read the folds use.
- **`params`** is the caller's query parameters as a plain `dict[str, str]`;
  the pack validates them and raises `ValidationError` for caller errors.
- **`PackReport`** carries either a `DocumentView` (the route renders the
  PDF — one visual language, pagination owned by the layout engine) or
  pre-rendered `csv` text (the route serves it as `text/csv`, attachment).
- One factory per key; a later registration replaces an earlier one.
  Unknown keys are 404s naming the registered ones.

The reference consumer is the statutory pack
(`ledgerforge/contrib/statutory.py`): PF, ESI and TDS statements folded
from `payroll.period_wages(resolver, period)` — the same fold the payslips
replay — with rates, ceilings, wage-component sets and TDS component names
as parameters (`tests/test_statutory_plugin.py` pins the whole path).

## Installation & the trust model (ADR-016)

Three channels, three trust bases:

- **pip path (trusted by installation):** declare an entry point in your package —
  ```toml
  [project.entry-points."ledgerforge.plugins"]
  my-gst-pack = "my_gst_pack:PLUGIN_MODULE"
  ```
  (the module containing `register`). The operator chose to install it; that
  is the trust decision.
- **Community index (signed):** `ledgerforge plugins install my-gst-pack`
  fetches the release from the [curated index](../../community/plugins/index.json),
  verifies its Ed25519 signature (release facts + artifact bytes) against the
  publisher key the index records, checks the `api_compat` floor, and lands
  plugin + `.sig` + publisher approval in the drop-in directory.
- **Drop-in path (proof required):** a single-file module on `LF_PLUGIN_PATH`
  (default: `./plugins`) loads **only** with proof — a valid `.py.sig` by an
  approved publisher, or an operator acknowledgement pinning the exact
  bytes (`ledgerforge plugins ack <module> --path ./plugins`). Everything
  else is refused at boot, loudly, without executing a line of it — and the
  refusal (with reason) is visible in `ledgerforge plugins list`, the
  `GET /api/v1/plugins` endpoint and the plugins screen (`Alt+X`).

A signature covers `{name, version, api_compat, sha256}` — it cannot be
replayed onto another release, a rename, or changed bytes. The manifest
must be statically readable for the signature path (plain string keywords);
a computed manifest loads only via acknowledgement. The same pack loaded
through two channels is refused the second time — transformers would run
twice on one posting.

Incompatible plugins (`api_compat` outside the supported range) are refused loudly at boot and at install — never silently misbehaving.

## Publishing to the community index (author-side)

1. Generate a publisher identity once: `ledgerforge plugins keygen --out ~/.ledgerforge-keys`
   (the private PEM never leaves your machine).
2. Keep the manifest static — `PLUGIN = PluginManifest(name=…, version=…, api_compat=…)`
   with plain string keywords — and declare the **oldest** plugin API your
   code runs on (it is a floor, not a pin).
3. Sign the release: `ledgerforge plugins sign my_pack.py --key ~/.ledgerforge-keys/publisher_ed25519.pem`
   writes `my_pack.py.sig`.
4. Open a pull request against `community/plugins/index.json` adding your
   artifact, its sha256, your public key and the signature — see the
   curation requirements in [community/plugins/README.md](../../community/plugins/README.md).
   The sample entry (`webhook-forwarder`) is a complete, signed example.

## What never changes

The engine re-validates the three financial laws **after** your transformer runs: unbalanced output, non-decimal amounts, or attempts to mutate history are impossible regardless of plugin quality. A plugin can veto or enrich a posting; it can never corrupt one.

## Worked pack: invoicing (`ledgerforge/contrib/invoicing.py`)

The first complete business document built purely on the runtime — the
roadmap's "invoicing as a plugin, not in core" promise kept. Two document
modes, both riding `meta` (which persists with the voucher and returns from
`GET /vouchers/{id}`, ADR-010):

**Sales invoice** — describe the commercial document; the transformer derives
the accounting:

```json
POST /api/v1/vouchers
{
  "voucher_type": "SAL", "voucher_date": "2026-11-06",
  "entries": [{"ledger_name": "Sales", "dc": "C", "amount": "0.01"}],
  "meta": {
    "invoice": {
      "customer": "Acme Traders",
      "items": [
        {"description": "Widgets", "qty": "10", "rate": "100.00", "tax_rate": "18"},
        {"description": "Installation", "qty": "1", "rate": "250.00", "tax_rate": "0"}
      ],
      "due_days": 30
    }
  }
}
```

What posts (the exact TallyPrime GST-sale shape): one debit to
`Acme Traders` (auto-created under *Sundry Debtors*) for the grand total;
one credit per item on its `revenue_ledger` (default `Sales`, under *Sales
Accounts*) with `Widgets × 10 @ 100.00` narrations; CGST/SGST split lines
per taxed item (under *Duties & Taxes*, the SGST line as remainder so the
split always sums to the tax exactly). The pack annotates
`meta.invoice.computed` — `subtotal`, `tax_total`, `grand_total`,
`due_date` — and the stored voucher **is** the invoice: items, rates and
totals all come back from `GET /vouchers/{id}`, immutable like the amounts
(Law 2 covers documents).

**AR receipt** — settle the invoice:

```json
{"receipt": {"customer": "Acme Traders", "amount": "1430.00",
             "bank_ledger": "HDFC Bank", "reference": "NEFT-42"}}
```

Debits the bank (auto-created under *Bank Accounts*), credits the customer.

Guard rails (all 422): tax rates outside the GST slabs `0/5/12/18/28`,
non-positive quantities, `meta.gst` + `meta.invoice` (double-tax),
`invoice` + `receipt` in one draft, and — as the policy-validator example —
document dates in the future (override with
`meta["policy:allow_future_document_dates"] = true`).

`ISSUED` (the after-commit seam) records every posted invoice/receipt with
its voucher number and customer: the spot an e-invoicing bridge or BI sync
subscribes to. The pack is registered as an entry-point
(`invoicing = "ledgerforge.contrib.invoicing"`), so it loads at boot
alongside the example — and it exercises the full ADR-009 contract:
masters resolve through the book-scoped `Resolver`, so the same customer
name in two books is two ledgers, never a leak.

## Worked pack: inventory (`ledgerforge/contrib/inventory.py`)

The second pack — and the first roadmap feature shipped with **zero
schema change** (ADR-011). Stock positions are *derived* by folding every
posted `meta.stock` document, the way reports fold entries; the pack
reads prior documents through `resolver.prior_documents` and serializes
same-item movements with `resolver.lock` (both plugin API 0.3).

Four document modes:

```json
{"stock": {"movement": "in", "party": "Omega Supplies",
  "items": [{"item": "Widgets", "qty": "10", "rate": "100.00"}]}}
// → Cr Omega Supplies (Sundry Creditors) 1000.00
//   Dr Widgets (Stock-in-Hand) 1000.00 · avg 100.0000

{"stock": {"movement": "out", "party": "Acme Traders",
  "items": [{"item": "Widgets", "qty": "3", "rate": "150.00"}]}}
// → Dr Acme Traders 450.00 · Cr Sales 450.00 (the commercial leg)
//   Dr Cost of Goods Sold 300.00 · Cr Widgets 300.00 (at avg cost)
//   computed: gross_margin 150.00

{"stock": {"movement": "transfer",
  "items": [{"item": "Widgets", "qty": "4", "to_ledger": "Widgets (Store B)"}]}}
// → Dr Widgets (Store B) / Cr Widgets — value moves, quantities don't

{"stock": {"movement": "recount",
  "items": [{"item": "Widgets", "qty": "9"}]}}
// → Cr Widgets 100.00 / Dr Stock Adjustment 100.00 (count delta at avg)
```

Valuation is a **moving weighted average** with closed arithmetic:
`value` only moves by posted 2-dp line amounts, so issuing the last unit
costs exactly the remaining value — positions empty to `0.00`, and the
transformer's computed annotations always agree with a later re-fold
(`inventory.position(resolver, item)` runs the same fold for bridges).
Reversals net to zero on stock: the fold skips an undone original, so
reversing a goods-out restores the stock just as the mirrored entries
restored the ledgers.

Guard rails (all 422): insufficient stock (`policy:allow_negative_stock`
overrides partial overdrafts; an empty position never issues), transfers
that would go negative, no-op recounts, mode conflicts (`stock` +
`gst`/`invoice`/`receipt`/`payroll`, vetoed by all packs), FX lines, future
dates (policy-overridable), and item names that would silently reuse an
unrelated existing ledger. `MOVEMENTS` (after-commit seam) records every
stock document — the warehouse-bridge hook point.

## Worked pack: payroll (`ledgerforge/contrib/payroll.py`)

The third pack — the second in a row with **zero schema change**
(ADR-012). Salaries are documents like invoices and stock: the *run*
accrues, the *payout* settles, and the derived state (year-to-date,
payable) is a deterministic fold over posted documents, never stored.
Plugin API 0.4 grew the one fact the fold needs
(`Resolver.fy_start_month`).

```json
{"payroll": {"mode": "run", "period": "2026-10",
  "employees": [{"employee": "Asha Verma", "components": [
    {"name": "Basic Salary", "amount": "50000.00", "kind": "earning"},
    {"name": "HRA", "amount": "20000.00", "kind": "earning"},
    {"name": "PF Payable", "amount": "1800.00", "kind": "withheld"},
    {"name": "Employer PF", "amount": "1800.00", "kind": "employer",
     "liability": "PF Payable"}
  ]}]}}
// → Dr Basic Salary 50000 · Dr HRA 20000 (Indirect Expenses)
//   Dr Employer PF 1800 · Cr PF Payable 1800+1800 (Duties & Taxes)
//   Cr Asha Verma 68200 (Provisions — the net-pay payable)
//   computed: per-employee gross/withheld/employer/net + fiscal-year YTD

{"payroll": {"mode": "payout", "employee": "Asha Verma",
  "amount": "68200.00", "bank": "HDFC Bank"}}
// → Dr Asha Verma 68200 · Cr HDFC Bank 68200
//   computed: payable_after 0.00
```

Components are pay heads with a `kind` — the TallyPrime taxonomy in
miniature: `earning` (Dr expense), `withheld` (Cr liability, reduces
net), `employer` (Dr expense + Cr a required `liability`; never touches
net). **YTD windows by the book's fiscal year** (a September payslip
never shows October); **payable is all-time** (accrued net minus
payouts — a balance never expires). One run per employee per period,
serialized by advisory locks; reversals net to zero on both views;
`payroll.ytd(resolver, employee)` and `payroll.payable(resolver,
employee)` replay the folds for bridges.

Guard rails (all 422): malformed periods (`YYYY-MM`), duplicate
employees/components, bad kinds, non-positive amounts, employer without
a liability, withheld exceeding gross, periods that have not started,
future dates, mode conflicts (vetoed by all packs), FX lines, and
employee names colliding with unrelated ledgers. `RUNS` (after-commit
seam) records every payroll document — the payslip/PF-bridge hook point.
