"""The GST compliance pack (India) — place of supply, HSN rate tables,
GSTR-ready statements and the e-invoicing outbox (ADR-017, issue #6);
grown to compliance depth in Phase 7 (ADR-028, issue #13).

The roadmap's worked example (``example_gst.py``) demonstrated the plugin
contracts by splitting an intra-state sale into CGST/SGST. This pack is that
example grown into a real localization plugin — the fifth business pack,
zero schema change, built on plugin API 0.6 pack reports:

- **Place of supply** (the CGST/SGST vs IGST decision): the supplier's state
  and the ``place_of_supply`` decide the levy — same state ⇒ CGST + SGST
  (halves of the slab, the SGST line the exact remainder), different state ⇒
  IGST (the whole slab). States are the GST state codes (the first two
  digits of a GSTIN), parseable as ``29``, ``29-Karnataka`` or a name.
- **HSN-wise rate tables with effective dates**: items may declare an HSN
  code and take their slab from a rate table at the voucher's date — rows
  carry ``effective_from``/``effective_to``, so a chapter's notification
  history (12% mobile phones until 2020-03-31, 18% from 2020-04-01) is
  resolved by the calendar, not by whoever edits the code. The shipped table
  is a small, documented seed; operators load their own notification history
  from ``LF_GST_HSN_JSON`` — rates change by notification, not by code
  (the ADR-014 discipline).
- **Document mode** ``meta.gst``: ``mode`` ``sale``/``purchase`` plus the
  return notes ``credit_note`` (a sales return: the party is credited, the
  revenue and tax reverse — netted into every outward figure) and
  ``debit_note`` (a purchase return: the party is debited, the input tax
  reverses — netted into every inward figure); ``party``, ``party_gstin``,
  ``supplier_state``, ``supplier_gstin`` (the registration the voucher
  belongs to — multi-GSTIN books segment every statement by it),
  ``invoice_no`` (the supplier's document number on inward documents — the
  2A/2B comparison's match key), ``place_of_supply`` and ``items``
  (``description``, ``hsn``, ``qty``, ``rate``, optional ``tax_rate`` and
  ``revenue_ledger``/``purchase_ledger``). The transformer derives the
  exact TallyPrime shape (party line at the grand total, one line per item
  on its revenue/purchase ledger, tax lines under *Duties & Taxes*) and
  annotates the totals into ``meta.gst.computed`` — persisted with the
  voucher (ADR-010), so the statements replay posted facts, never re-decide
  them. The legacy quick-tax shape (``meta.gst = {"rate": ...}``) still
  works and now honors ``place_of_supply`` too.
- **GSTR-ready statements** (plugin API 0.6): ``GET /api/v1/reports/pack/
  gstr1?period=YYYY-MM`` — the outward B2B invoice register, GSTIN-wise,
  with taxable value and the CGST/SGST/IGST split, returns netted — and
  ``GET /api/v1/reports/pack/gst-hsn?period=YYYY-MM`` — the HSN summary
  (per HSN × rate: quantity, taxable value, tax split). The Phase 7 depth
  joins them: **``gstr3b``** (the monthly summary's own statement —
  outward, inward, the eligible ITC and the net payable from the same
  folds), **``gstr9``** (the annual return's scaffolding — the twelve
  months folded into the annexure tables) and **``gst-recon``** (the
  GSTR-2A/2B comparison: the books' inward register against the imported
  portal statement — matched / mismatched / missing-in-books /
  missing-in-portal, the ITC both sides claim). All accept ``?gstin=`` to
  scope one registration. ``?format=csv`` serves the machine format,
  ``?format=json`` the structured payload; the default renders the PDF
  through the shared layout engine. A *reporting bridge*, not a
  portal-certified file generator: every statement carries the applied
  rules in its footer.
- **The e-invoicing (IRN) outbox**: every posted B2B sale (a document-mode
  sale carrying ``party_gstin``) is appended — after commit, out of the
  posting path — as an IRP-ready envelope to the JSONL outbox named by
  ``LF_GST_OUTBOX``. ``lf-irn-bridge`` (``python -m ledgerforge.contrib.
  irn_bridge``) ships the outbox to the operator's IRP adapter **in a
  separate process** and records the returned IRN/ack — latency-tolerant
  integration stays out-of-process by contract (D3), while the tax itself
  ran inside the posting transaction where Law 1 lives.
- **The e-way-bill outbox** (the IRN pattern applied to movement, ADR-028):
  every posted document-mode voucher at or above the movement threshold
  (``LF_GST_EWB_THRESHOLD``, default ₹50,000 — Rule 138's own line) is
  appended after commit as a movement envelope — the document facts plus
  the ``meta.gst.eway`` block the operator captured at dispatch
  (transporter, vehicle, from, distance) — to the JSONL outbox named by
  ``LF_GST_EWB_OUTBOX``; ``lf-ewb-bridge`` ships it to the operator's
  adapter and records the returned EWB number. ``policy:skip_ewb`` opts a
  voucher out.

All arithmetic is exact ``Decimal`` (Law 3); tax rounds half-up to 2 places
and the SGST line is the remainder, so CGST + SGST == tax exactly; the
statements fold the same document sequence with the same reversal netting
as every other pack, so a GSTR figure that disagrees with the ledger is
impossible, not merely tested-for. Books are strangers (ADR-009).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ..errors import ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentSection,
    DocumentView,
    DraftLine,
    PackReport,
    PluginManifest,
    PriorDocument,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.gst")

PLUGIN = PluginManifest(name="gst", version="0.2.0", api_compat="0.6")

GSTR1_KEY = "gstr1"
GST_HSN_KEY = "gst-hsn"
GSTR3B_KEY = "gstr3b"
GSTR9_KEY = "gstr9"
GST_RECON_KEY = "gst-recon"
META_KEY = "gst"

#: The GST slabs — the whitelist that keeps a fat-fingered rate from
#: silently posting the wrong tax (same discipline as the invoicing pack).
TAX_SLABS = ("0", "5", "12", "18", "28")

#: Seed groups (ledgerforge/seeding.py) — masters resolve only through the
#: book-scoped Resolver, so every ledger the pack mints stays in-book.
DEBTOR_GROUP = "Sundry Debtors"
CREDITOR_GROUP = "Sundry Creditors"
REVENUE_GROUP = "Sales Accounts"
PURCHASE_GROUP = "Purchase Accounts"
TAX_GROUP = "Duties & Taxes"
DEFAULT_REVENUE_LEDGER = "Sales"
DEFAULT_PURCHASE_LEDGER = "Purchases"
DEFAULT_CREDIT_NOTE_LEDGER = "Sales Returns"
DEFAULT_DEBIT_NOTE_LEDGER = "Purchase Returns"

#: The e-way-bill movement threshold (Rule 138's own line): document-mode
#: vouchers at or above it queue a movement envelope, unless the operator
#: opts the voucher out (``policy:skip_ewb``) or moves the deployment's
#: line (``LF_GST_EWB_THRESHOLD``).
EWB_THRESHOLD = Decimal("50000")

#: Every voucher posted while this pack is loaded (observability for
#: tests/demos — the contract the example plugin demonstrated first).
POSTED_EVENTS: list[dict] = []

CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_TWO = Decimal(2)

#: GST state codes — the first two digits of a GSTIN (the standard list;
#: 22 Gujarat, 27 Maharashtra, 29 Karnataka, 33 Tamil Nadu, 36 Telangana…).
STATE_CODES: dict[str, str] = {
    "01": "Jammu & Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman & Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}


# ----------------------------------------------------------- state parsing
def parse_state(value: object, field: str) -> tuple[str, str]:
    """``"29"`` / ``"29-Karnataka"`` / ``"karnataka"`` → ``(code, name)``.

    Unknown codes and names are refused loudly: a wrong state code is a
    wrong levy (intra vs inter), not a cosmetic detail.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValidationError(f"gst: {field} is required (a GST state code or name)")
    candidate = raw.upper()
    # a bare code, or the canonical "NN-Name" form
    head = candidate.split("-", 1)[0].strip()
    if head.isdigit() and head in STATE_CODES:
        return head, STATE_CODES[head]
    # by name, case-insensitively
    for code, name in STATE_CODES.items():
        if candidate == name.upper():
            return code, name
    raise ValidationError(
        f"gst: {field} {raw!r} is not a GST state — pass a state code (e.g. 29), "
        "a 'NN-Name' pair (29-Karnataka) or a state name"
    )


def supply_kind(supplier_state: str, place_of_supply: str) -> str:
    """The levy decision: same state ⇒ ``intra`` (CGST + SGST), different
    state ⇒ ``inter`` (IGST). Code equality handles the union territories
    correctly: a UT with its own code is its own place of supply."""
    return "intra" if supplier_state == place_of_supply else "inter"


# -------------------------------------------------------- the HSN rate table
@dataclass(frozen=True)
class HsnRate:
    """One HSN chapter's slab over a date range — notification history as
    data. ``hsn`` is a 2–8 digit prefix: an item's HSN matches by prefix
    (the HSN hierarchy), and ``effective_to`` (inclusive) closes superseded
    rows."""

    hsn: str
    description: str
    rate: Decimal  # the slab in percent, e.g. Decimal("18")
    effective_from: date
    effective_to: date | None = None


#: The seed table — a small, deliberately illustrative slice of public
#: notification history (see ADR-017): the launch slabs of four stable
#: chapters and the one famous revision (mobile phones, 12% → 18% from
#: 2020-04-01). Operators load their full history via LF_GST_HSN_JSON.
DEFAULT_HSN_TABLE: tuple[HsnRate, ...] = (
    HsnRate("0401", "Milk & cream, not concentrated nor sweetened", Decimal("0"), date(2017, 7, 1)),
    HsnRate("1511", "Palm oil & its fractions", Decimal("5"), date(2017, 7, 1)),
    HsnRate("4901", "Printed books, brochures & similar", Decimal("0"), date(2017, 7, 1)),
    HsnRate("7317", "Nails, tacks, pins & staples of iron/steel", Decimal("18"), date(2017, 7, 1)),
    HsnRate(
        "8517",
        "Telephones for cellular networks",
        Decimal("12"),
        date(2017, 7, 1),
        date(2020, 3, 31),
    ),
    HsnRate("8517", "Telephones for cellular networks", Decimal("18"), date(2020, 4, 1)),
)


def load_hsn_table(path: str | Path) -> tuple[HsnRate, ...]:
    """Parse an operator rate table (JSON list of rows with ``hsn``,
    ``description``, ``rate``, ``effective_from`` and optional
    ``effective_to``) — the notification history this book runs on."""
    try:
        rows = json.loads(Path(path).read_text())
    except OSError as exc:
        raise ValidationError(f"gst: cannot read the HSN table {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"gst: the HSN table {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(rows, list) or not rows:
        raise ValidationError(f"gst: the HSN table {path!r} must be a non-empty JSON list")
    table: list[HsnRate] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValidationError(f"gst: HSN table row {i} must be an object")
        hsn = str(row.get("hsn") or "").strip()
        if not hsn.isdigit() or not 2 <= len(hsn) <= 8:
            raise ValidationError(f"gst: HSN table row {i}: hsn must be 2-8 digits, got {hsn!r}")
        try:
            rate = Decimal(str(row.get("rate")))
        except ArithmeticError:
            raise ValidationError(f"gst: HSN table row {i}: rate must be a number") from None
        if rate < 0:
            raise ValidationError(f"gst: HSN table row {i}: rate must be >= 0")
        try:
            eff_from = date.fromisoformat(str(row.get("effective_from") or ""))
            eff_to = (
                date.fromisoformat(str(row["effective_to"])) if row.get("effective_to") else None
            )
        except ValueError:
            raise ValidationError(
                f"gst: HSN table row {i}: effective_from/effective_to must be YYYY-MM-DD"
            ) from None
        if eff_to is not None and eff_to < eff_from:
            raise ValidationError(f"gst: HSN table row {i}: effective_to precedes effective_from")
        table.append(
            HsnRate(
                hsn=hsn,
                description=str(row.get("description") or hsn),
                rate=rate,
                effective_from=eff_from,
                effective_to=eff_to,
            )
        )
    return tuple(table)


def hsn_table() -> tuple[HsnRate, ...]:
    """The rate table in force: the operator's ``LF_GST_HSN_JSON`` file when
    set, else the shipped seed. Read at posting time — a notification file
    the operator drops takes effect on the next voucher, no restart."""
    path = os.environ.get("LF_GST_HSN_JSON", "").strip()
    return load_hsn_table(path) if path else DEFAULT_HSN_TABLE


def rate_for(table: tuple[HsnRate, ...], hsn: str, on: date) -> Decimal | None:
    """The slab for ``hsn`` at ``on``: prefix-matching rows valid on the
    date (``effective_from <= on`` and, when closed, ``on <= effective_to``),
    the most recent ``effective_from`` winning; ``None`` when the table does
    not know the chapter. Two different slabs equally in force is a broken
    table — refused loudly, never guessed."""
    digits = "".join(ch for ch in hsn if ch.isdigit())
    if not digits:
        return None
    candidates = [
        row
        for row in table
        if digits.startswith(row.hsn)
        and row.effective_from <= on
        and (row.effective_to is None or on <= row.effective_to)
    ]
    if not candidates:
        return None
    latest = max(row.effective_from for row in candidates)
    winners = [row for row in candidates if row.effective_from == latest]
    rates = {row.rate for row in winners}
    if len(rates) > 1:
        pretty = ", ".join(sorted(f"{row.rate}% from {row.effective_from}" for row in winners))
        raise ValidationError(
            f"gst: HSN {hsn!r} has conflicting slabs in force on {on.isoformat()}: {pretty}"
        )
    return winners[0].rate


def hsn_description(table: tuple[HsnRate, ...], hsn: str, on: date) -> str:
    """The description of the row ``rate_for`` picked — for statements."""
    digits = "".join(ch for ch in hsn if ch.isdigit())
    candidates = [
        row
        for row in table
        if digits.startswith(row.hsn)
        and row.effective_from <= on
        and (row.effective_to is None or on <= row.effective_to)
    ]
    if not candidates:
        return ""
    latest = max(row.effective_from for row in candidates)
    return next(row.description for row in candidates if row.effective_from == latest)


# ------------------------------------------------------------ GSTIN checks
_GSTIN_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def check_gstin(value: object, field: str) -> str:
    """A 15-character GSTIN: 2-digit state code + PAN + entity code + 'Z' +
    checksum. Format-checked (and the state prefix must be a known state);
    the mod-36 checksum is the portal's to verify — printed as posted,
    never invented."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if (
        len(raw) != 15
        or not raw[:2].isdigit()
        or raw[:2] not in STATE_CODES
        or not raw[2:7].isalpha()
        or not raw[7:11].isdigit()
        or not raw[11:12].isalpha()
        or raw[13] != "Z"
        or any(ch not in _GSTIN_CHARS for ch in raw)
    ):
        raise ValidationError(
            f"gst: {field} {value!r} is not a 15-character GSTIN "
            "(state code + PAN + entity code + Z + checksum)"
        )
    return raw


# ------------------------------------------------------------- the helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"gst: {field} must be a number, got {value!r}") from None


def _resolve_slab(item: dict, index: int, table: tuple[HsnRate, ...], on: date) -> Decimal:
    """One item's slab: the explicit ``tax_rate`` when given (whitelisted to
    the slabs), else the HSN table at the voucher date; both given and in
    disagreement is a fat-finger veto, never a guess."""
    where = f"gst.items[{index}]"
    explicit = item.get("tax_rate")
    hsn = str(item.get("hsn") or "").strip()
    table_rate: Decimal | None = None
    if hsn:
        table_rate = rate_for(table, hsn, on)
        if table_rate is None:
            raise ValidationError(
                f"gst: {where}.hsn {hsn!r} is not in the rate table at "
                f"{on.isoformat()} — pass tax_rate explicitly or extend LF_GST_HSN_JSON"
            )
    if explicit is not None:
        raw = str(explicit).strip()
        if raw not in TAX_SLABS:
            raise ValidationError(
                f"gst: {where}.tax_rate {raw!r} is not a GST slab (one of {', '.join(TAX_SLABS)})"
            )
        declared = Decimal(raw)
        if table_rate is not None and table_rate != declared:
            raise ValidationError(
                f"gst: {where}: tax_rate {raw}% disagrees with the HSN table "
                f"({table_rate}% for {hsn!r} at {on.isoformat()}) — fix the document"
            )
        return declared
    if table_rate is not None:
        return table_rate
    raise ValidationError(f"gst: {where}: pass tax_rate (a slab) or hsn (a table-known chapter)")


def _tax_split(tax: Decimal, kind: str) -> tuple[Decimal, Decimal, Decimal]:
    """(cgst, sgst, igst) for one item's tax: intra splits half-up with the
    SGST line as the exact remainder; inter is the whole slab as IGST."""
    if kind == "inter":
        return Decimal("0.00"), Decimal("0.00"), tax
    cgst = _money(tax / _TWO)
    return cgst, tax - cgst, Decimal("0.00")


def _supplier_state(doc: dict, field: str = "gst.supplier_state") -> str:
    """The registrant's state: the document's word, else the deployment's
    ``LF_GST_SUPPLIER_STATE``. A compliance fact — one place, explicit."""
    if doc.get("supplier_state"):
        return parse_state(doc["supplier_state"], field)[0]
    env = os.environ.get("LF_GST_SUPPLIER_STATE", "").strip()
    if env:
        return parse_state(env, field)[0]
    raise ValidationError(
        f"gst: {field} is required — pass supplier_state on the document or set "
        "LF_GST_SUPPLIER_STATE (the registrant's GST state code)"
    )


# ---------------------------------------------------------- the transformer
#: The document modes and their accounting shape. Every mode is one
#: TallyPrime voucher: the party line at the grand total on one side, the
#: item lines and tax lines on the other — the notes are the same shape
#: with the sides exchanged (a return is the document walked backwards),
#: so the fold nets them with a sign instead of a separate maths.
#: ``doc_type`` is the GST register's name for the mode (INV/CN/DN).
_MODE_SHAPES: dict[str, dict] = {
    "sale": {
        "direction": "outward",
        "sign": 1,
        "item_dc": "C",
        "party_group": DEBTOR_GROUP,
        "default_ledger": DEFAULT_REVENUE_LEDGER,
        "item_group": REVENUE_GROUP,
        "ledger_key": "revenue_ledger",
        "label": "sale to",
    },
    "credit_note": {
        "direction": "outward",
        "sign": -1,
        "item_dc": "D",
        "party_group": DEBTOR_GROUP,
        "default_ledger": DEFAULT_CREDIT_NOTE_LEDGER,
        "item_group": REVENUE_GROUP,
        "ledger_key": "revenue_ledger",
        "label": "credit note to",
    },
    "purchase": {
        "direction": "inward",
        "sign": 1,
        "item_dc": "D",
        "party_group": CREDITOR_GROUP,
        "default_ledger": DEFAULT_PURCHASE_LEDGER,
        "item_group": PURCHASE_GROUP,
        "ledger_key": "purchase_ledger",
        "label": "purchase from",
    },
    "debit_note": {
        "direction": "inward",
        "sign": -1,
        "item_dc": "C",
        "party_group": CREDITOR_GROUP,
        "default_ledger": DEFAULT_DEBIT_NOTE_LEDGER,
        "item_group": PURCHASE_GROUP,
        "ledger_key": "purchase_ledger",
        "label": "debit note to",
    },
}

DOC_TYPE_FOR_MODE = {"sale": "INV", "purchase": "INV", "credit_note": "CN", "debit_note": "DN"}

#: Books document types the portal's row types pair with: a books purchase
#: pairs the supplier's invoice; a books debit note (our return) pairs the
#: supplier's credit note — and occasionally their debit note — the same
#: physical document named from both ends (ADR-028).
_DOC_TYPE_PAIRS = {"purchase": ("INV",), "debit_note": ("CN", "DN")}


def build_gst_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    """``meta.gst`` in either shape — the full document mode or the legacy
    quick-tax — becomes the exact accounting TallyPrime would post."""
    doc = draft.meta.get("gst")
    if doc is None:
        return draft
    if not isinstance(doc, dict):
        raise ValidationError("gst: meta.gst must be an object")
    if any(ln.currency is not None for ln in draft.lines):
        # Tax computes on base amounts only; an FX line through the split
        # would drop its snapshot trio — refuse loudly (the example pack's
        # discipline, kept).
        raise ValidationError(
            "gst: meta.gst is not supported on foreign-currency lines "
            "(post the tax lines explicitly, or extend this plugin)"
        )
    if "items" in doc or "mode" in doc:
        return _build_document_mode(draft, resolver, doc)
    return _build_quick_tax(draft, resolver, doc)


def _registrant_gstin(doc: dict, field: str = "gst.supplier_gstin") -> str:
    """The registration the voucher belongs to: the document's word, else
    the deployment's ``LF_GST_SUPPLIER_GSTIN``. A multi-GSTIN book (one
    company, registrations in several states) segments every statement by
    this fact — one place, explicit, exactly the supplier_state rule."""
    if doc.get("supplier_gstin"):
        return check_gstin(doc["supplier_gstin"], field)
    env = os.environ.get("LF_GST_SUPPLIER_GSTIN", "").strip()
    if env:
        return check_gstin(env, field)
    return ""


def _build_document_mode(draft: VoucherDraft, resolver: Resolver, doc: dict) -> VoucherDraft:
    mode = str(doc.get("mode") or "sale").strip().lower()
    if mode not in _MODE_SHAPES:
        raise ValidationError(
            f"gst: mode must be one of {', '.join(_MODE_SHAPES)}, got {doc.get('mode')!r}"
        )
    shape = _MODE_SHAPES[mode]
    for other in ("invoice", "receipt", "stock", "payroll"):
        if draft.meta.get(other) is not None:
            raise ValidationError(
                f"gst: meta.gst and meta.{other} are mutually exclusive "
                "(one document mode per voucher)"
            )
    party = str(doc.get("party") or "").strip()
    if not party:
        raise ValidationError("gst: party is required")
    gstin = check_gstin(doc.get("party_gstin"), "gst.party_gstin")
    supplier_gstin = _registrant_gstin(doc)
    supplier_state = _supplier_state(doc)
    pos_code, pos_name = (
        parse_state(doc["place_of_supply"], "gst.place_of_supply")
        if doc.get("place_of_supply")
        else (supplier_state, STATE_CODES[supplier_state])
    )
    kind = supply_kind(supplier_state, pos_code)
    # the supplier's document number on inward documents (and our own
    # reference on outward ones when the operator names it) — the 2A/2B
    # comparison's match key, stored as a posted fact
    invoice_no = str(doc.get("invoice_no") or doc.get("doc_no") or "").strip()

    items = doc.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("gst: items must be a non-empty list")
    table = hsn_table()
    on = draft.voucher_date

    lines: list[DraftLine] = []
    subtotal = Decimal("0.00")
    cgst_total = Decimal("0.00")
    sgst_total = Decimal("0.00")
    igst_total = Decimal("0.00")

    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"gst: items[{i}] must be an object")
        description = str(item.get("description") or "").strip() or f"item {i}"
        qty = _decimal(item.get("qty", 1), f"gst.items[{i}].qty")
        rate = _decimal(item.get("rate", 0), f"gst.items[{i}].rate")
        if qty <= 0:
            raise ValidationError(f"gst: items[{i}].qty must be > 0")
        if rate < 0:
            raise ValidationError(f"gst: items[{i}].rate must be >= 0")
        slab = _resolve_slab(item, i, table, on)
        # the resolved slab is posted on the item: the statements replay it
        # as a stored fact instead of re-deciding it (Law 2 discipline)
        item["tax_rate"] = str(slab.normalize())
        amount = _money(qty * rate)
        tax = _money(amount * slab / _HUNDRED)
        cgst, sgst, igst = _tax_split(tax, kind)

        ledger = resolver.ensure_ledger(
            str(item.get(shape["ledger_key"]) or shape["default_ledger"]),
            shape["item_group"],
        )
        lines.append(
            DraftLine(
                dc=shape["item_dc"],
                amount=amount,
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"{description} × {qty} @ {rate}",
            )
        )

        if tax > 0:
            for amount_tax, base_name in ((cgst, "CGST"), (sgst, "SGST"), (igst, "IGST")):
                if amount_tax == 0:
                    continue
                # TallyPrime's naming: CGST/SGST/IGST Payable outward,
                # Input CGST/SGST/IGST inward — all under Duties & Taxes.
                # A return rides the same ledgers on the opposite side.
                outward = shape["direction"] == "outward"
                ledger_name = f"{base_name} Payable" if outward else f"Input {base_name}"
                tax_ledger = resolver.ensure_ledger(ledger_name, TAX_GROUP)
                lines.append(
                    DraftLine(
                        dc=shape["item_dc"],
                        amount=amount_tax,
                        ledger_id=tax_ledger.id,
                        ledger_name=tax_ledger.name,
                        narration=f"{base_name} {slab.normalize():f}% on {description}",
                    )
                )

        subtotal += amount
        cgst_total += cgst
        sgst_total += sgst
        igst_total += igst

    tax_total = cgst_total + sgst_total + igst_total
    grand_total = subtotal + tax_total
    party_ledger = resolver.ensure_ledger(party, shape["party_group"])
    narration = f"{shape['label']} {party}"
    if gstin:
        narration += f" ({gstin})"

    lines.insert(
        0,
        DraftLine(
            dc="D" if shape["item_dc"] == "C" else "C",
            amount=grand_total,
            ledger_id=party_ledger.id,
            ledger_name=party_ledger.name,
            narration=narration,
        ),
    )

    doc["invoice_no"] = invoice_no
    doc["supplier_gstin"] = supplier_gstin
    doc["computed"] = {
        "mode": mode,
        "supply": kind,
        "supplier_state": supplier_state,
        "supplier_gstin": supplier_gstin,
        "place_of_supply": pos_code,
        "place_of_supply_name": pos_name,
        "party_gstin": gstin,
        "invoice_no": invoice_no,
        "subtotal": str(subtotal),
        "cgst_total": str(cgst_total),
        "sgst_total": str(sgst_total),
        "igst_total": str(igst_total),
        "tax_total": str(tax_total),
        "grand_total": str(grand_total),
    }
    return draft.with_lines(lines)


def _build_quick_tax(draft: VoucherDraft, resolver: Resolver, doc: dict) -> VoucherDraft:
    """The legacy shape (``{"rate": ...}``): tax on the first credit line,
    split by place of supply — CGST/SGST intra (the example's behavior,
    unchanged), IGST inter (new)."""
    rate = doc.get("rate", 0)
    if rate in (None, ""):
        raise ValidationError("gst: quick-tax needs rate (a slab) or the document mode")
    raw = str(rate).strip()
    if raw not in TAX_SLABS:
        raise ValidationError(
            f"gst: rate {raw!r} is not a GST slab (one of {', '.join(TAX_SLABS)})"
        )
    slab = Decimal(raw)
    if slab <= 0:
        return draft
    credit_lines = [ln for ln in draft.lines if ln.dc == "C"]
    debit_lines = [ln for ln in draft.lines if ln.dc == "D"]
    if not credit_lines or not debit_lines:
        raise ValidationError("gst: voucher needs at least one debit and one credit line")

    kind: str = "intra"
    if doc.get("place_of_supply"):
        supplier_state = _supplier_state(doc)
        pos_code, _ = parse_state(doc["place_of_supply"], "gst.place_of_supply")
        kind = supply_kind(supplier_state, pos_code)
        doc["computed"] = {
            "supply": kind,
            "supplier_state": supplier_state,
            "place_of_supply": pos_code,
            "quick_tax": str(slab),
        }

    base = credit_lines[0].amount
    tax = _money(base * slab / _HUNDRED)
    cgst, sgst, igst = _tax_split(tax, kind)
    extras: list[DraftLine] = []
    for amount_tax, base_name in ((cgst, "CGST"), (sgst, "SGST"), (igst, "IGST")):
        if amount_tax == 0:
            continue
        ledger = resolver.ensure_ledger(f"{base_name} Payable", TAX_GROUP)
        extras.append(
            DraftLine(dc="C", amount=amount_tax, ledger_id=ledger.id, narration=base_name)
        )

    lines = list(draft.lines)
    first_debit = debit_lines[0]
    lines[lines.index(first_debit)] = DraftLine(
        dc="D",
        amount=first_debit.amount + tax,
        ledger_id=first_debit.ledger_id,
        ledger_name=first_debit.ledger_name,
        narration=first_debit.narration,
    )
    return draft.with_lines(lines + extras)


# ------------------------------------------------------------ the validator
def no_weekend_posting(draft: VoucherDraft, resolver: Resolver) -> None:
    """The example pack's policy, kept: ``policy:no_weekends`` blocks
    weekend posting (override with ``policy:no_weekends=false``)."""
    if draft.meta.get("policy:no_weekends") and draft.voucher_date.weekday() >= 5:
        raise ValidationError(
            f"Policy: posting on {draft.voucher_date.isoformat()} is blocked "
            "(weekend; override with policy:no_weekends=false)"
        )


# ------------------------------------------------- the e-invoicing outbox
def irp_envelope(event_data: dict) -> dict | None:
    """The IRP-ready payload for one posted voucher, or ``None`` when the
    posting is not a queueable B2B sale: only document-mode sales carrying a
    ``party_gstin`` need an IRN, and ``policy:skip_irn`` opts out (an
    operator's registered composition dealer, a credit-note correction the
    adapter handles its own way, …)."""
    meta = event_data.get("meta") or {}
    doc = meta.get(META_KEY)
    if not isinstance(doc, dict):
        return None
    if meta.get("policy:skip_irn"):
        return None
    computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
    if computed.get("mode") != "sale" or not computed.get("party_gstin"):
        return None
    items = []
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        qty = _decimal(item.get("qty", 1), "qty")
        rate = _decimal(item.get("rate", 0), "rate")
        slab = _decimal(item.get("tax_rate", 0), "tax_rate")
        amount = _money(qty * rate)
        items.append(
            {
                "description": str(item.get("description") or ""),
                "hsn": str(item.get("hsn") or ""),
                "qty": str(qty),
                "rate": str(rate),
                "tax_rate": str(slab),
                "amount": str(amount),
                "tax": str(_money(amount * slab / _HUNDRED)),
            }
        )
    return {
        # the operator's adapter contract (ADR-017), not the government
        # schema — the ASP adapter translates to the portal's format
        "kind": "lf-gst-invoice/1",
        "queued_at": None,  # stamped by the queue writer
        "book_id": event_data.get("book_id"),
        "voucher_id": event_data.get("voucher_id"),
        "voucher_no": event_data.get("voucher_no"),
        "voucher_date": event_data.get("voucher_date"),
        "supplier": {
            "state": computed.get("supplier_state", ""),
            "gstin": str(doc.get("supplier_gstin") or ""),
        },
        "buyer": {"gstin": computed.get("party_gstin", ""), "name": str(doc.get("party") or "")},
        "place_of_supply": computed.get("place_of_supply", ""),
        "items": items,
        "totals": {
            "taxable": computed.get("subtotal", ""),
            "cgst": computed.get("cgst_total", ""),
            "sgst": computed.get("sgst_total", ""),
            "igst": computed.get("igst_total", ""),
            "grand_total": computed.get("grand_total", ""),
        },
    }


def queue_for_irp(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    """The after-commit half of the e-invoicing bridge: append every B2B
    sale's IRP envelope to the ``LF_GST_OUTBOX`` JSONL file. Out-of-process
    by contract (D3): the outbox is the only hand-off — no network I/O ever
    runs inside the posting path, and a missing/unwritable outbox is logged
    and isolated (an event handler must never break posting)."""
    from datetime import UTC, datetime

    outbox = os.environ.get("LF_GST_OUTBOX", "").strip()
    if not outbox:
        return
    envelope = irp_envelope(event.data)
    if envelope is None:
        return
    envelope["queued_at"] = datetime.now(UTC).isoformat()
    path = Path(outbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    logger.info("queued %s for the IRP adapter", event.data.get("voucher_no"))


def record_posted(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    POSTED_EVENTS.append({"name": event.name, "voucher_no": event.data.get("voucher_no")})
    logger.info("voucher posted: %s", event.data.get("voucher_no"))


# ------------------------------------------------------------ the GSTR fold
@dataclass(frozen=True)
class GstInvoice:
    """One outward document's GSTR-1 row — the posted facts, replayed.
    ``doc_type`` is the register's name for it (INV/CN/DN); the register's
    totals net the notes (a credit note walks the sale backwards)."""

    gstin: str
    party: str
    voucher_no: str
    voucher_date: date
    place_of_supply: str
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    rates: tuple[str, ...]
    doc_type: str = "INV"


@dataclass(frozen=True)
class HsnRow:
    """One HSN × rate accumulation — outward or inward."""

    direction: str  # "outward" | "inward"
    hsn: str
    description: str
    rate: Decimal
    qty: Decimal
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal


@dataclass(frozen=True)
class InwardDoc:
    """One inward document as the 2A/2B comparison sees it (ADR-028): the
    supplier's facts from the books — their GSTIN, their document number
    (``invoice_no``, the match key), the mode's register type (a purchase
    is INV, a debit note DN), and the amounts **signed** (a debit note
    reduces the inward mass, exactly as it reduces the ITC)."""

    supplier_gstin: str
    party: str
    doc_type: str
    doc_no: str
    doc_date: date
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    voucher_no: str


def _net_gst_documents(resolver: Resolver) -> list[PriorDocument]:
    """The document-store fold's shared discipline (ADR-011/012/014): every
    ``meta.gst`` voucher, oldest first, with whole-voucher reversals netted
    out (a reversed original drops, exactly like the payroll fold)."""
    documents = resolver.prior_documents(META_KEY)
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    return [d for d in documents if d.reversal_of is None and d.voucher_id not in reversed_ids]


def _in_period(document: PriorDocument, period: str) -> bool:
    return document.voucher_date.isoformat()[:7] == period


def _doc_registrant(doc: dict, computed: dict) -> str:
    """The registration a posted document belongs to — the computed fact
    (a Phase 7 voucher), else the document's word, else the deployment
    default, else "" (an unregistrant book: unfiltered, honestly)."""
    gstin = str(computed.get("supplier_gstin") or doc.get("supplier_gstin") or "").strip()
    if gstin:
        return gstin
    return check_gstin(os.environ.get("LF_GST_SUPPLIER_GSTIN", ""), "LF_GST_SUPPLIER_GSTIN")


def _gstin_filter(params: dict[str, str]) -> str:
    """The ``?gstin=`` scope a caller asked for, validated — a wrong GSTIN
    is a wrong statement, not a cosmetic detail."""
    raw = str(params.get("gstin") or "").strip()
    return check_gstin(raw.upper(), "gstin") if raw else ""


def _period_documents(
    resolver: Resolver, period: str, gstin: str = ""
) -> list[tuple[PriorDocument, dict, dict, dict]]:
    """One period's document-mode vouchers — ``(document, doc, computed,
    mode shape)`` tuples, oldest first — scoped to one registration when
    asked. Quick-tax vouchers ride along with shape ``{}``: the callers
    that cannot use them count them, exactly as before."""
    out: list[tuple[PriorDocument, dict, dict, dict]] = []
    for d in _net_gst_documents(resolver):
        if not _in_period(d, period):
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
        if gstin and _doc_registrant(doc, computed) != gstin:
            continue
        mode = str(computed.get("mode") or doc.get("mode") or "")
        shape = _MODE_SHAPES.get(mode)
        if shape is None and not doc.get("items"):
            shape = {}  # quick-tax: present but featureless
        out.append((d, doc, computed, shape))
    return out


def _document_amounts(doc: dict, computed: dict, shape: dict) -> tuple[Decimal, ...]:
    """One document's replayed ``(taxable, cgst, sgst, igst)`` from its
    posted items — the same figures the ledger posted, by construction —
    signed by the mode (a return negates)."""
    sign = shape.get("sign", 1)
    kind = str(computed.get("supply") or "intra")
    taxable = Decimal("0.00")
    cgst = Decimal("0.00")
    sgst = Decimal("0.00")
    igst = Decimal("0.00")
    for item in doc.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            qty = Decimal(str(item.get("qty", 1)))
            rate = Decimal(str(item.get("rate", 0)))
            slab = Decimal(str(item.get("tax_rate", 0)))
        except ArithmeticError:
            continue  # a document this pack did not shape — not ours
        amount = _money(qty * rate)
        tax = _money(amount * slab / _HUNDRED)
        ic, isg, iig = _tax_split(tax, kind)
        taxable += amount
        cgst += ic
        sgst += isg
        igst += iig
    if sign < 0:
        return -taxable, -cgst, -sgst, -igst
    return taxable, cgst, sgst, igst


def gstr1_invoices(
    resolver: Resolver,
    period: str,
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> tuple[list[GstInvoice], int]:
    """The outward B2B register of one ``YYYY-MM``: every document-mode
    outward voucher carrying a GSTIN (B2B) — invoices and credit notes,
    the notes netting the totals — amounts replayed from the posted items
    and the immutable ``computed`` block. Returns the rows plus the count
    of quick-tax vouchers the statement deliberately excludes (they carry
    no HSN/party facts — the register cannot invent them)."""
    q = quantize or _money
    invoices: list[GstInvoice] = []
    quick_tax = 0
    for d, doc, computed, shape in _period_documents(resolver, period, gstin):
        if not shape:
            if doc.get("rate") is not None or computed.get("quick_tax") is not None:
                quick_tax += 1
            continue
        if shape["direction"] != "outward":
            continue
        gstin_party = str(computed.get("party_gstin") or doc.get("party_gstin") or "").strip()
        if not gstin_party:
            continue  # B2C: the register is B2B — counted in the summary, not listed
        taxable, cgst, sgst, igst = _document_amounts(doc, computed, shape)
        rates: list[str] = []
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            try:
                label = f"{Decimal(str(item.get('tax_rate', 0))).normalize():f}"
            except ArithmeticError:
                continue
            if label not in rates:
                rates.append(label)
        mode = str(computed.get("mode") or doc.get("mode"))
        invoices.append(
            GstInvoice(
                gstin=gstin_party,
                party=str(doc.get("party") or ""),
                voucher_no=d.voucher_no,
                voucher_date=d.voucher_date,
                place_of_supply=str(computed.get("place_of_supply") or ""),
                taxable=q(taxable),
                cgst=q(cgst),
                sgst=q(sgst),
                igst=q(igst),
                rates=tuple(rates),
                doc_type=DOC_TYPE_FOR_MODE[mode],
            )
        )
    invoices.sort(key=lambda inv: (inv.voucher_date, inv.voucher_no))
    return invoices, quick_tax


def inward_documents(
    resolver: Resolver,
    period: str,
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> tuple[list[InwardDoc], int]:
    """The inward B2B register of one ``YYYY-MM`` (ADR-028): every
    document-mode inward voucher carrying a supplier GSTIN — purchases and
    debit notes — with the supplier's own document number when the books
    carry it (``invoice_no``), amounts signed so a return reduces the
    inward mass. Returns the rows plus the count of inward documents that
    cannot be matched at all (no party GSTIN, or no supplier document
    number): honest blanks, never invented keys."""
    q = quantize or _money
    rows: list[InwardDoc] = []
    unmatchable = 0
    for d, doc, computed, shape in _period_documents(resolver, period, gstin):
        if not shape or shape["direction"] != "inward":
            continue
        supplier = str(computed.get("party_gstin") or doc.get("party_gstin") or "").strip()
        mode = str(computed.get("mode") or doc.get("mode"))
        invoice_no = str(computed.get("invoice_no") or doc.get("invoice_no") or "").strip()
        if not supplier or not invoice_no:
            unmatchable += 1
            continue
        taxable, cgst, sgst, igst = _document_amounts(doc, computed, shape)
        rows.append(
            InwardDoc(
                supplier_gstin=supplier,
                party=str(doc.get("party") or ""),
                doc_type=DOC_TYPE_FOR_MODE[mode],
                doc_no=invoice_no,
                doc_date=d.voucher_date,
                taxable=q(taxable),
                cgst=q(cgst),
                sgst=q(sgst),
                igst=q(igst),
                voucher_no=d.voucher_no,
            )
        )
    rows.sort(key=lambda r: (r.doc_date, r.doc_no))
    return rows, unmatchable


def hsn_rows(
    resolver: Resolver,
    period: str,
    side: str = "outward",
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> list[HsnRow]:
    """The HSN summary of one ``YYYY-MM``: per (HSN, rate) — quantity,
    taxable value and the tax split, outward and/or inward (``side=``),
    returns netting their side's mass. Quick-tax vouchers are excluded:
    no HSN, no summary row — a footnote says so, honestly."""
    q = quantize or _money
    wanted_sides = {
        "outward": ("outward",),
        "inward": ("inward",),
        "both": ("outward", "inward"),
    }.get(side, ("outward", "inward"))
    rows: dict[tuple[str, str, str], HsnRow] = {}
    for d, doc, computed, shape in _period_documents(resolver, period, gstin):
        if not shape:
            continue
        if shape["direction"] not in wanted_sides:
            continue
        kind = str(computed.get("supply") or "intra")
        sign = shape["sign"]
        table = hsn_table()
        for item in doc.get("items") or []:
            if not isinstance(item, dict):
                continue
            try:
                qty = Decimal(str(item.get("qty", 1)))
                rate = Decimal(str(item.get("rate", 0)))
                slab = Decimal(str(item.get("tax_rate", 0)))
            except ArithmeticError:
                continue
            hsn = str(item.get("hsn") or "").strip() or "—"
            description = str(item.get("description") or "").strip()
            if hsn != "—" and not description:
                description = hsn_description(table, hsn, d.voucher_date)
            amount = _money(qty * rate)
            tax = _money(amount * slab / _HUNDRED)
            ic, isg, iig = _tax_split(tax, kind)
            key = (shape["direction"], hsn, f"{slab.normalize():f}")
            row = rows.get(key)
            if row is None:
                rows[key] = HsnRow(
                    direction=shape["direction"],
                    hsn=hsn,
                    description=description,
                    rate=slab,
                    qty=qty * sign,
                    taxable=amount * sign,
                    cgst=ic * sign,
                    sgst=isg * sign,
                    igst=iig * sign,
                )
            else:
                rows[key] = HsnRow(
                    direction=row.direction,
                    hsn=row.hsn,
                    description=row.description or description,
                    rate=row.rate,
                    qty=row.qty + qty * sign,
                    taxable=row.taxable + amount * sign,
                    cgst=row.cgst + ic * sign,
                    sgst=row.sgst + isg * sign,
                    igst=row.igst + iig * sign,
                )
    # the edge rounding (Law 3): sums accumulate exact, quantize once at the
    # fold's output — round=rupee is honored, never silently ignored
    quantized = [
        HsnRow(
            direction=r.direction,
            hsn=r.hsn,
            description=r.description,
            rate=r.rate,
            qty=r.qty,
            taxable=q(r.taxable),
            cgst=q(r.cgst),
            sgst=q(r.sgst),
            igst=q(r.igst),
        )
        for r in rows.values()
    ]
    return sorted(quantized, key=lambda r: (r.direction, r.hsn, r.rate))


# ------------------------------------------------------------ report params
def _required_period(params: dict[str, str]) -> str:
    period = str(params.get("period") or "").strip()
    if not period:
        raise ValidationError("gst: pass ?period=YYYY-MM — the tax month the statement covers")
    year, month = period[:4], period[5:7]
    if len(period) != 7 or not year.isdigit() or not month.isdigit() or not 1 <= int(month) <= 12:
        raise ValidationError(f"gst: period must be YYYY-MM, got {period!r}")
    return period


def _format(params: dict[str, str]) -> str:
    fmt = str(params.get("format") or "pdf").strip().lower()
    if fmt not in ("pdf", "csv", "json"):
        raise ValidationError(f"gst: format must be one of pdf, csv, json, got {fmt!r}")
    return fmt


def _required_year(params: dict[str, str]) -> str:
    """The GSTR-9's own window: a full year ``YYYY`` — the twelve months
    the annual return folds."""
    year = str(params.get("period") or params.get("year") or "").strip()
    if len(year) != 4 or not year.isdigit():
        raise ValidationError(f"gst: GSTR-9 needs ?period=YYYY (the financial year), got {year!r}")
    return year


def _recon_kind(params: dict[str, str]) -> str:
    """Which portal statement the comparison reads: 2B (the static,
    ITC-as-stated input) by default, 2A on ask."""
    kind = str(params.get("kind") or "2b").strip().lower()
    if kind not in ("2a", "2b"):
        raise ValidationError(f"gst: kind must be one of 2a, 2b, got {kind!r}")
    return kind


def _side(params: dict[str, str]) -> str:
    side = str(params.get("side") or "outward").strip().lower()
    if side not in ("outward", "inward", "both"):
        raise ValidationError(f"gst: side must be one of outward, inward, both, got {side!r}")
    return side


def _quantizer(params: dict[str, str]):
    mode = str(params.get("round") or "cent").strip().lower()
    if mode == "cent":
        return _money
    if mode == "rupee":
        return lambda value: value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    raise ValidationError(f"gst: round must be one of cent, rupee, got {mode!r}")


_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _period_label(period: str) -> str:
    try:
        return f"{_MONTHS[int(period[5:7]) - 1]} {period[:4]}"
    except (ValueError, IndexError):
        return period


# ------------------------------------------------------------------- CSV
GSTR1_CSV_HEADER = (
    "Type",
    "GSTIN",
    "Party",
    "Invoice",
    "Date",
    "POS",
    "Rates",
    "Taxable Value",
    "CGST",
    "SGST",
    "IGST",
)

GST_HSN_CSV_HEADER = (
    "Direction",
    "HSN",
    "Description",
    "Rate",
    "Qty",
    "Taxable Value",
    "CGST",
    "SGST",
    "IGST",
)


def _csv(header: tuple[str, ...], rows: list[list[str]]) -> str:
    import csv
    import io

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def gstr1_csv(invoices: list[GstInvoice]) -> str:
    rows = [
        [
            inv.doc_type,
            inv.gstin,
            inv.party,
            inv.voucher_no,
            inv.voucher_date.isoformat(),
            inv.place_of_supply,
            "+".join(inv.rates),
            str(inv.taxable),
            str(inv.cgst),
            str(inv.sgst),
            str(inv.igst),
        ]
        for inv in invoices
    ]
    return _csv(GSTR1_CSV_HEADER, rows)


def hsn_csv(rows: list[HsnRow]) -> str:
    return _csv(
        GST_HSN_CSV_HEADER,
        [
            [
                row.direction,
                row.hsn,
                row.description,
                f"{row.rate.normalize():f}",
                f"{row.qty.normalize():f}",
                str(row.taxable),
                str(row.cgst),
                str(row.sgst),
                str(row.igst),
            ]
            for row in rows
        ],
    )


# ------------------------------------------------------------------- PDF
def _supplier_label() -> str:
    env = os.environ.get("LF_GST_SUPPLIER_STATE", "").strip()
    if env:
        try:
            code, name = parse_state(env, "LF_GST_SUPPLIER_STATE")
            return f"{code} ({name})"
        except ValidationError:
            return env
    return "unset (documents carry their own supplier_state)"


def gstr1_view(
    period: str, invoices: list[GstInvoice], quick_tax: int, gstin: str = ""
) -> DocumentView:
    total = lambda pick: sum((pick(i) for i in invoices), Decimal("0.00"))  # noqa: E731
    rows = [
        (
            inv.doc_type,
            inv.gstin,
            inv.party,
            inv.voucher_no,
            inv.voucher_date.isoformat(),
            inv.place_of_supply,
            "+".join(inv.rates) or "—",
            format_amount(inv.taxable),
            format_amount(inv.cgst),
            format_amount(inv.sgst),
            format_amount(inv.igst),
        )
        for inv in invoices
    ]
    notes = sum(1 for inv in invoices if inv.doc_type != "INV")
    footnotes = [
        "The outward B2B register: document-mode outward vouchers carrying a "
        "GSTIN, folded from posted meta.gst vouchers with reversals netted "
        "(Law 2); credit notes net the totals — a return walks its document "
        "backwards.",
        f"Supplier state: {_supplier_label()}.",
        (
            f"Registration: {gstin} (the ?gstin= scope this register ran under)."
            if gstin
            else "Registration: all (pass ?gstin= to scope one registration of a multi-GSTIN book)."
        ),
        "Quick-tax vouchers (meta.gst.rate) carry no HSN/party facts and are not "
        "in this register — a blank is honest, an invented GSTIN is not.",
        "GSTINs are format-checked, checksums unverified — verify against the "
        "portal's current template (GSTR-1) before uploading.",
    ]
    if quick_tax:
        footnotes.insert(
            3,
            f"{quick_tax} quick-tax voucher(s) posted this period are excluded (no "
            "HSN/party facts).",
        )
    if notes:
        footnotes.insert(
            1,
            f"{notes} credit note(s) in this register reduce the totals (the "
            "CDNR rows of the portal's own template).",
        )
    if not invoices:
        footnotes.append("No B2B outward documents posted for this period — a nil statement.")

    return DocumentView(
        title="GSTR-1 — outward supplies",
        subtitle=f"B2B invoice register, returns netted — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("B2B documents", str(len(invoices))),
            ("Credit notes", str(notes)),
            ("Register", "B2B (GSTIN-wise)"),
        ),
        sections=(
            DocumentSection(
                title="B2B invoices & credit notes",
                columns=(
                    DocumentColumn("Type", width=0.35),
                    DocumentColumn("GSTIN", width=1.3),
                    DocumentColumn("Party", width=1.55),
                    DocumentColumn("Invoice", width=0.95),
                    DocumentColumn("Date", width=0.75),
                    DocumentColumn("POS", width=0.4),
                    DocumentColumn("Rate", width=0.45, align="right"),
                    DocumentColumn("Taxable", width=0.95, align="right"),
                    DocumentColumn("CGST", width=0.75, align="right"),
                    DocumentColumn("SGST", width=0.75, align="right"),
                    DocumentColumn("IGST", width=0.75, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Total taxable value", format_amount(total(lambda i: i.taxable))),
            ("Total CGST", format_amount(total(lambda i: i.cgst))),
            ("Total SGST", format_amount(total(lambda i: i.sgst))),
            ("Total IGST", format_amount(total(lambda i: i.igst))),
            (
                "Total tax",
                format_amount(total(lambda i: i.cgst + i.sgst + i.igst)),
            ),
        ),
        footnotes=tuple(footnotes),
        filename=f"GSTR1 {period}",
    )


def hsn_view(period: str, rows: list[HsnRow], side: str) -> DocumentView:
    def table(direction: str) -> DocumentSection:
        wanted = [r for r in rows if r.direction == direction]
        return DocumentSection(
            title=f"{direction.capitalize()} supplies",
            columns=(
                DocumentColumn("HSN", width=0.8),
                DocumentColumn("Description", width=2.0),
                DocumentColumn("Rate", width=0.5, align="right"),
                DocumentColumn("Qty", width=0.7, align="right"),
                DocumentColumn("Taxable", width=1.0, align="right"),
                DocumentColumn("CGST", width=0.85, align="right"),
                DocumentColumn("SGST", width=0.85, align="right"),
                DocumentColumn("IGST", width=0.85, align="right"),
            ),
            rows=tuple(
                (
                    r.hsn,
                    r.description,
                    f"{r.rate.normalize():f}%",
                    f"{r.qty.normalize():f}",
                    format_amount(r.taxable),
                    format_amount(r.cgst),
                    format_amount(r.sgst),
                    format_amount(r.igst),
                )
                for r in wanted
            ),
        )

    sections = []
    if side in ("outward", "both"):
        sections.append(table("outward"))
    if side in ("inward", "both"):
        sections.append(table("inward"))

    total = lambda pick: sum((pick(r) for r in rows), Decimal("0.00"))  # noqa: E731
    return DocumentView(
        title="GST HSN summary",
        subtitle=f"HSN-wise outward & inward supplies — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("Side", side),
            ("HSN × rate rows", str(len(rows))),
        ),
        sections=tuple(sections),
        totals=(
            ("Total taxable value", format_amount(total(lambda r: r.taxable))),
            ("Total CGST", format_amount(total(lambda r: r.cgst))),
            ("Total SGST", format_amount(total(lambda r: r.sgst))),
            ("Total IGST", format_amount(total(lambda r: r.igst))),
        ),
        footnotes=(
            "Per HSN × rate: quantity and value replayed from posted items with "
            "reversals netted (Law 2); the slab is the one resolved at posting "
            "time (effective-dated HSN table, ADR-017).",
            "UQC (unit-of-quantity) codes are not captured — the portal asks for "
            "them; this summary is a reconciliation aid, not an upload file.",
            "Items without an HSN print as a dash — the register cannot classify "
            "what the document did not declare.",
            "Verify against the portal's current template before uploading.",
        ),
        filename=f"GST HSN summary {period}",
    )


# ------------------------------------------------------------ the factories
def gstr1_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The GSTR-1 outward B2B register of one tax month — invoices and
    credit notes, returns netted — PDF (default), CSV or the JSON payload."""
    period = _required_period(params)
    _format(params)  # validates pdf/csv/json — an unknown format never renders
    gstin = _gstin_filter(params)
    invoices, quick_tax = gstr1_invoices(resolver, period, _quantizer(params), gstin)
    data = {
        "period": period,
        "gstin": gstin or None,
        "quick_tax_excluded": quick_tax,
        "invoices": [
            {
                "doc_type": inv.doc_type,
                "gstin": inv.gstin,
                "party": inv.party,
                "voucher_no": inv.voucher_no,
                "voucher_date": inv.voucher_date.isoformat(),
                "place_of_supply": inv.place_of_supply,
                "rates": list(inv.rates),
                "taxable": str(inv.taxable),
                "cgst": str(inv.cgst),
                "sgst": str(inv.sgst),
                "igst": str(inv.igst),
            }
            for inv in invoices
        ],
    }
    return PackReport(
        title=f"GSTR-1 {period}",
        filename=f"GSTR1 {period}",
        view=gstr1_view(period, invoices, quick_tax, gstin),
        csv=gstr1_csv(invoices),
        data=data,
    )


def hsn_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The HSN summary of one tax month — PDF (default), CSV or the JSON
    payload."""
    period = _required_period(params)
    _format(params)  # validates pdf/csv/json — an unknown format never renders
    side = _side(params)
    gstin = _gstin_filter(params)
    rows = hsn_rows(resolver, period, side, _quantizer(params), gstin)
    data = {
        "period": period,
        "side": side,
        "gstin": gstin or None,
        "rows": [
            {
                "direction": r.direction,
                "hsn": r.hsn,
                "description": r.description,
                "rate": f"{r.rate.normalize():f}",
                "qty": f"{r.qty.normalize():f}",
                "taxable": str(r.taxable),
                "cgst": str(r.cgst),
                "sgst": str(r.sgst),
                "igst": str(r.igst),
            }
            for r in rows
        ],
    }
    return PackReport(
        title=f"GST HSN summary {period}",
        filename=f"GST HSN summary {period}",
        view=hsn_view(period, rows, side),
        csv=hsn_csv(rows),
        data=data,
    )


# ------------------------------------------- the Phase 7 statements (ADR-028)
def _zero() -> dict[str, Decimal]:
    return {
        "taxable": Decimal("0.00"),
        "cgst": Decimal("0.00"),
        "sgst": Decimal("0.00"),
        "igst": Decimal("0.00"),
    }


def gstr3b_summary(
    resolver: Resolver,
    period: str,
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> dict:
    """The monthly summary's own statement (ADR-028): outward taxable and
    tax split intra/inter with returns netted, the eligible ITC from the
    inward register (purchases less debit notes), and the net payable —
    one fold, the same numbers the GSTR-1 register and the HSN summary
    carry, so a 3B that disagrees with the ledger is impossible, not
    merely tested-for. Reverse-charge and exempt supplies are not facts
    this pack captures: they report zero with a footnote, never a guess."""
    q = quantize or _money
    heads = {
        ("outward", "intra"): _zero(),
        ("outward", "inter"): _zero(),
        ("inward", "intra"): _zero(),
        ("inward", "inter"): _zero(),
    }
    counts = {
        "outward": 0,
        "outward_returns": 0,
        "inward": 0,
        "inward_returns": 0,
        "quick_tax": 0,
    }
    for _d, doc, computed, shape in _period_documents(resolver, period, gstin):
        if not shape:
            if doc.get("rate") is not None or computed.get("quick_tax") is not None:
                counts["quick_tax"] += 1
            continue
        kind = "inter" if str(computed.get("supply")) == "inter" else "intra"
        head = heads[(shape["direction"], kind)]
        taxable, cgst, sgst, igst = _document_amounts(doc, computed, shape)
        head["taxable"] += taxable
        head["cgst"] += cgst
        head["sgst"] += sgst
        head["igst"] += igst
        if shape["sign"] < 0:
            counts[shape["direction"] + "_returns"] += 1
        else:
            counts[shape["direction"]] += 1

    def _head_out(head: dict) -> dict:
        return {
            "taxable": str(q(head["taxable"])),
            "cgst": str(q(head["cgst"])),
            "sgst": str(q(head["sgst"])),
            "igst": str(q(head["igst"])),
        }

    # the liability is the outward heads only — the inward heads are the
    # ITC's own mass, credited in section 4, never added to the payable
    outward_tax = sum(
        (
            h["cgst"] + h["sgst"] + h["igst"]
            for (side, _kind), h in heads.items()
            if side == "outward"
        ),
        Decimal("0.00"),
    )
    itc = (
        heads[("inward", "intra")]["cgst"]
        + heads[("inward", "intra")]["sgst"]
        + heads[("inward", "intra")]["igst"]
        + heads[("inward", "inter")]["cgst"]
        + heads[("inward", "inter")]["sgst"]
        + heads[("inward", "inter")]["igst"]
    )
    return {
        "period": period,
        "gstin": gstin or None,
        "outward": {
            "intra": _head_out(heads[("outward", "intra")]),
            "inter": _head_out(heads[("outward", "inter")]),
            "documents": counts["outward"],
            "returns": counts["outward_returns"],
        },
        "inward": {
            "intra": _head_out(heads[("inward", "intra")]),
            "inter": _head_out(heads[("inward", "inter")]),
            "documents": counts["inward"],
            "returns": counts["inward_returns"],
        },
        "itc_available": str(q(itc)),
        "outward_tax": str(q(outward_tax)),
        "net_payable": str(q(outward_tax - itc)),
        "quick_tax_excluded": counts["quick_tax"],
    }


GSTR3B_CSV_HEADER = ("Section", "Particulars", "Taxable Value", "CGST", "SGST", "IGST")


def _head_row(section: str, label: str, head: dict) -> list[str]:
    return [section, label, head["taxable"], head["cgst"], head["sgst"], head["igst"]]


def gstr3b_csv(data: dict) -> str:
    o, i = data["outward"], data["inward"]
    rows = [
        _head_row("3.1", "Taxable intra-state supplies (net of returns)", o["intra"]),
        _head_row("3.1", "Taxable inter-state supplies (net of returns)", o["inter"]),
        _head_row(
            "3.1",
            "Nil-rated / exempt / non-GST (not captured)",
            {"taxable": "0.00", "cgst": "0.00", "sgst": "0.00", "igst": "0.00"},
        ),
        _head_row(
            "3.1",
            "Inward supplies liable to reverse charge (not captured)",
            {"taxable": "0.00", "cgst": "0.00", "sgst": "0.00", "igst": "0.00"},
        ),
        _head_row("4", "All other ITC available (inward register, net of returns)", i["intra"]),
        _head_row("4", "ITC on inter-state inward (net of returns)", i["inter"]),
    ]
    rows.append(["4", "Net ITC available", "", "", "", data["itc_available"]])
    rows.append(["6", "Net tax payable (outward tax − net ITC)", "", "", "", data["net_payable"]])
    return _csv(GSTR3B_CSV_HEADER, rows)


def _amount_head(label: str, head: dict) -> tuple[str, ...]:
    return (
        label,
        format_amount(Decimal(head["taxable"])),
        format_amount(Decimal(head["cgst"])),
        format_amount(Decimal(head["sgst"])),
        format_amount(Decimal(head["igst"])),
    )


def gstr3b_view(data: dict) -> DocumentView:
    o, i = data["outward"], data["inward"]
    itc = Decimal(data["itc_available"])
    outward_tax = Decimal(data["outward_tax"])
    net_payable = Decimal(data["net_payable"])
    footnotes = [
        "The monthly summary's own statement: the same folds the GSTR-1 register "
        "and the HSN summary carry, netted of returns — the ledger's numbers or "
        "no numbers.",
        "Reverse-charge imports (3.1(d)) and exempt inward supplies (5) are not "
        "facts this pack captures: post them explicitly when they apply — a "
        "zero here is a blank, not a verdict.",
        "ITC reversal (4B) and set-off (Table 6's payment rows) happen at "
        "settlement, outside the books: report the ledger's eligible figure and "
        "reconcile before paying.",
    ]
    if data["quick_tax_excluded"]:
        footnotes.insert(
            1,
            f"{data['quick_tax_excluded']} quick-tax voucher(s) excluded (no "
            "HSN/party facts — they cannot enter a summary they never declared).",
        )
    if net_payable < 0:
        footnotes.append(
            "Net payable is negative: tax credit carried forward — verify the "
            "month's set-off before claiming it on the portal."
        )
    return DocumentView(
        title="GSTR-3B — the monthly summary",
        subtitle=f"Outward, inward, ITC & net payable — {_period_label(data['period'])}",
        fields=(
            ("Period", _period_label(data["period"])),
            ("Registration", data["gstin"] or "all (pass ?gstin= to scope one)"),
            ("Outward documents", f"{o['documents']} (+{o['returns']} returns netted)"),
            ("Inward documents", f"{i['documents']} (+{i['returns']} returns netted)"),
        ),
        sections=(
            DocumentSection(
                title="3.1 Outward supplies & inward reverse charge",
                columns=(
                    DocumentColumn("Particulars", width=2.6),
                    DocumentColumn("Taxable", width=1.0, align="right"),
                    DocumentColumn("CGST", width=0.8, align="right"),
                    DocumentColumn("SGST", width=0.8, align="right"),
                    DocumentColumn("IGST", width=0.8, align="right"),
                ),
                rows=(
                    _amount_head("Taxable intra-state supplies (net of returns)", o["intra"]),
                    _amount_head("Taxable inter-state supplies (net of returns)", o["inter"]),
                    (
                        "Nil-rated / exempt / non-GST (not captured)",
                        "—",
                        "—",
                        "—",
                        "—",
                    ),
                    (
                        "Inward supplies liable to reverse charge (not captured)",
                        "—",
                        "—",
                        "—",
                        "—",
                    ),
                ),
            ),
            DocumentSection(
                title="4 Eligible ITC",
                columns=(
                    DocumentColumn("Particulars", width=2.6),
                    DocumentColumn("Taxable", width=1.0, align="right"),
                    DocumentColumn("CGST", width=0.8, align="right"),
                    DocumentColumn("SGST", width=0.8, align="right"),
                    DocumentColumn("IGST", width=0.8, align="right"),
                ),
                rows=(
                    _amount_head("All other ITC available (inward, net of returns)", i["intra"]),
                    _amount_head("ITC on inter-state inward (net of returns)", i["inter"]),
                    (
                        "ITC reversed (settlement, not modelled)",
                        "—",
                        "—",
                        "—",
                        "—",
                    ),
                ),
            ),
        ),
        totals=(
            ("Total outward tax", format_amount(outward_tax)),
            ("Net ITC available", format_amount(itc)),
            ("Net tax payable (outward − ITC)", format_amount(net_payable)),
        ),
        footnotes=tuple(footnotes),
        filename=f"GSTR3B {data['period']}",
    )


def gstr3b_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The GSTR-3B monthly summary — PDF (default), CSV or the JSON payload."""
    period = _required_period(params)
    _format(params)  # validates pdf/csv/json — an unknown format never renders
    gstin = _gstin_filter(params)
    data = gstr3b_summary(resolver, period, _quantizer(params), gstin)
    return PackReport(
        title=f"GSTR-3B {period}",
        filename=f"GSTR3B {period}",
        view=gstr3b_view(data),
        csv=gstr3b_csv(data),
        data=data,
    )


def gstr9_tables(
    resolver: Resolver,
    year: str,
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> dict:
    """The annual return's scaffolding (ADR-028): the year's twelve months
    folded monthly — outward taxable and tax, the ITC — plus the annual
    totals the annexure tables declare. Honest about what the books hold:
    portal-only cells (declared-vs-actual turnover, the HSN summary
    table, imports) are scaffolding to fill from the portal, never
    invented numbers."""
    q = quantize or _money
    months: list[dict] = []
    for m in range(1, 13):
        period = f"{year}-{m:02d}"
        data = gstr3b_summary(resolver, period, None, gstin)
        taxable = Decimal(data["outward"]["intra"]["taxable"]) + Decimal(
            data["outward"]["inter"]["taxable"]
        )
        cgst = Decimal(data["outward"]["intra"]["cgst"]) + Decimal(data["outward"]["inter"]["cgst"])
        sgst = Decimal(data["outward"]["intra"]["sgst"]) + Decimal(data["outward"]["inter"]["sgst"])
        igst = Decimal(data["outward"]["intra"]["igst"]) + Decimal(data["outward"]["inter"]["igst"])
        months.append(
            {
                "month": period,
                "outward_taxable": str(q(taxable)),
                "cgst": str(q(cgst)),
                "sgst": str(q(sgst)),
                "igst": str(q(igst)),
                "itc": data["itc_available"],
            }
        )
    totals = {
        "outward_taxable": str(
            q(sum((Decimal(m["outward_taxable"]) for m in months), Decimal("0.00")))
        ),
        "cgst": str(q(sum((Decimal(m["cgst"]) for m in months), Decimal("0.00")))),
        "sgst": str(q(sum((Decimal(m["sgst"]) for m in months), Decimal("0.00")))),
        "igst": str(q(sum((Decimal(m["igst"]) for m in months), Decimal("0.00")))),
        "itc": str(q(sum((Decimal(m["itc"]) for m in months), Decimal("0.00")))),
    }
    totals["net_payable"] = str(
        q(
            Decimal(totals["cgst"])
            + Decimal(totals["sgst"])
            + Decimal(totals["igst"])
            - Decimal(totals["itc"])
        )
    )
    return {"year": year, "gstin": gstin or None, "months": months, "totals": totals}


GSTR9_CSV_HEADER = ("Month", "Outward Taxable", "CGST", "SGST", "IGST", "ITC")


def gstr9_csv(data: dict) -> str:
    rows = [
        [
            m["month"],
            m["outward_taxable"],
            m["cgst"],
            m["sgst"],
            m["igst"],
            m["itc"],
        ]
        for m in data["months"]
    ]
    t = data["totals"]
    rows.append(["TOTAL", t["outward_taxable"], t["cgst"], t["sgst"], t["igst"], t["itc"]])
    return _csv(GSTR9_CSV_HEADER, rows)


def gstr9_view(data: dict) -> DocumentView:
    t = data["totals"]
    rows = [
        (
            m["month"],
            format_amount(Decimal(m["outward_taxable"])),
            format_amount(Decimal(m["cgst"])),
            format_amount(Decimal(m["sgst"])),
            format_amount(Decimal(m["igst"])),
            format_amount(Decimal(m["itc"])),
        )
        for m in data["months"]
    ]
    return DocumentView(
        title="GSTR-9 — the annual return (scaffolding)",
        subtitle=f"The year in twelve months — {data['year']}",
        fields=(
            ("Year", data["year"]),
            ("Registration", data["gstin"] or "all (pass ?gstin= to scope one)"),
            (
                "Months with documents",
                str(
                    sum(
                        1
                        for m in data["months"]
                        if Decimal(m["outward_taxable"]) != 0 or Decimal(m["itc"]) != 0
                    )
                ),
            ),
        ),
        sections=(
            DocumentSection(
                title="The year, month by month (Table 4 & 6's own source)",
                columns=(
                    DocumentColumn("Month", width=0.9),
                    DocumentColumn("Taxable", width=1.1, align="right"),
                    DocumentColumn("CGST", width=0.9, align="right"),
                    DocumentColumn("SGST", width=0.9, align="right"),
                    DocumentColumn("IGST", width=0.9, align="right"),
                    DocumentColumn("ITC", width=1.0, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Annual outward taxable (Table 4)", format_amount(Decimal(t["outward_taxable"]))),
            ("Annual CGST", format_amount(Decimal(t["cgst"]))),
            ("Annual SGST", format_amount(Decimal(t["sgst"]))),
            ("Annual IGST", format_amount(Decimal(t["igst"]))),
            ("Annual ITC (Table 6)", format_amount(Decimal(t["itc"]))),
            ("Net payable (the year's own sum)", format_amount(Decimal(t["net_payable"]))),
        ),
        footnotes=(
            "The annual return's scaffolding: twelve monthly folds of the same "
            "registers GSTR-1/3B render — the books' declaration, replayed.",
            "Portal-only tables (declared-vs-actual turnover, the HSN summary, "
            "imports, amendments) are not invented here: fill them from the "
            "portal's own records — this is the scaffolding, not the filing.",
            "Verify every figure against the portal's current GSTR-9 template before uploading.",
        ),
        filename=f"GSTR9 {data['year']}",
    )


def gstr9_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The GSTR-9 annual scaffolding — PDF (default), CSV or the JSON payload."""
    year = _required_year(params)
    _format(params)  # validates pdf/csv/json — an unknown format never renders
    gstin = _gstin_filter(params)
    data = gstr9_tables(resolver, year, _quantizer(params), gstin)
    return PackReport(
        title=f"GSTR-9 {year}",
        filename=f"GSTR9 {year}",
        view=gstr9_view(data),
        csv=gstr9_csv(data),
        data=data,
    )


# ------------------------------------------------ the 2A/2B comparison (ADR-028)
@dataclass(frozen=True)
class ReconRow:
    """One comparison outcome: a document as the books hold it and as the
    portal states it — one side blank when only one holds it, the reason
    naming every disagreement exactly."""

    state: str  # matched | mismatched | missing_books | missing_portal
    supplier_gstin: str
    party: str
    doc_type: str
    doc_no: str
    doc_date: str
    books_taxable: Decimal | None
    books_tax: Decimal | None
    portal_taxable: Decimal | None
    portal_tax: Decimal | None
    reason: str


def gst_reconciliation(
    resolver: Resolver,
    period: str,
    kind: str = "2b",
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
    gstin: str = "",
) -> dict:
    """The ITC reconciliation (ADR-028): the books' inward B2B register —
    purchases and debit notes, keyed on the supplier's GSTIN and their own
    document number — against the imported portal statement's rows. Every
    document lands in exactly one bucket: **matched** (key and amounts
    agree), **mismatched** (the key agrees, the facts do not — the reason
    names the disagreement), **missing in books** (the portal knows a
    supplier the books do not) or **missing in portal** (the books claim a
    document the portal has not drafted). Inward documents without the
    supplier's GSTIN or invoice number cannot be matched at all — counted,
    never guessed."""
    q = quantize or _money
    books, unmatchable = inward_documents(resolver, period, q, gstin)
    portal = resolver.gst_portal_lines(kind, period, gstin or None)

    books_by_key: dict[tuple[str, str], InwardDoc] = {}
    for b in books:
        books_by_key.setdefault((b.supplier_gstin, b.doc_no), b)
    portal_by_key: dict[tuple[str, str], object] = {}
    for p in portal:
        portal_by_key.setdefault((p.supplier_gstin, p.doc_no), p)

    rows: list[ReconRow] = []
    for key in sorted(set(books_by_key) | set(portal_by_key)):
        b = books_by_key.get(key)
        p = portal_by_key.get(key)
        if b is not None and p is not None:
            books_taxable = q(b.taxable)
            books_tax = q(b.cgst + b.sgst + b.igst)
            portal_taxable = q(p.taxable)
            portal_tax = q(p.igst + p.cgst + p.sgst)
            expected = _DOC_TYPE_PAIRS.get("purchase" if b.doc_type == "INV" else "debit_note", ())
            reasons = []
            if p.doc_type not in expected:
                reasons.append(f"type {b.doc_type}↔{p.doc_type}")
            # magnitudes: a books return negates its amounts (it reduces the
            # inward mass), the portal states the document's face — the same
            # physical document, priced from both ends
            if abs(books_taxable) != portal_taxable:
                reasons.append(f"taxable {books_taxable}↔{portal_taxable}")
            if abs(books_tax) != portal_tax:
                reasons.append(f"tax {books_tax}↔{portal_tax}")
            rows.append(
                ReconRow(
                    state="matched" if not reasons else "mismatched",
                    supplier_gstin=key[0],
                    party=b.party or p.party,
                    doc_type=b.doc_type,
                    doc_no=key[1],
                    doc_date=b.doc_date.isoformat(),
                    books_taxable=books_taxable,
                    books_tax=books_tax,
                    portal_taxable=portal_taxable,
                    portal_tax=portal_tax,
                    reason="; ".join(reasons),
                )
            )
        elif b is not None:
            rows.append(
                ReconRow(
                    state="missing_portal",
                    supplier_gstin=b.supplier_gstin,
                    party=b.party,
                    doc_type=b.doc_type,
                    doc_no=b.doc_no,
                    doc_date=b.doc_date.isoformat(),
                    books_taxable=q(b.taxable),
                    books_tax=q(b.cgst + b.sgst + b.igst),
                    portal_taxable=None,
                    portal_tax=None,
                    reason="the books claim it, the portal has not drafted it",
                )
            )
        else:
            rows.append(
                ReconRow(
                    state="missing_books",
                    supplier_gstin=p.supplier_gstin,
                    party=p.party,
                    doc_type=p.doc_type,
                    doc_no=p.doc_no,
                    doc_date=p.doc_date.isoformat(),
                    books_taxable=None,
                    books_tax=None,
                    portal_taxable=q(p.taxable),
                    portal_tax=q(p.igst + p.cgst + p.sgst),
                    reason="the portal drafted it, the books do not hold it",
                )
            )

    counts = {
        "matched": sum(1 for r in rows if r.state == "matched"),
        "mismatched": sum(1 for r in rows if r.state == "mismatched"),
        "missing_books": sum(1 for r in rows if r.state == "missing_books"),
        "missing_portal": sum(1 for r in rows if r.state == "missing_portal"),
    }
    itc_books = sum((r.books_tax for r in rows if r.books_tax is not None), Decimal("0.00"))
    # the portal's own ITC arithmetic: invoices and supplier debit notes
    # add, the supplier's credit notes subtract — the buyer's 2B nets CDNR
    itc_portal = Decimal("0.00")
    for p in portal:
        if portal_by_key.get((p.supplier_gstin, p.doc_no)) is not p:
            continue  # a twin the fold did not pick — not the portal's row
        tax = q(p.igst + p.cgst + p.sgst)
        itc_portal += -tax if p.doc_type == "CN" else tax
    portal_ineligible = [
        p
        for p in portal
        if not p.itc_available
        and (p.supplier_gstin, p.doc_no) in portal_by_key
        and portal_by_key[(p.supplier_gstin, p.doc_no)] is p
    ]
    ineligible_tax = sum((q(p.igst + p.cgst + p.sgst) for p in portal_ineligible), Decimal("0.00"))
    return {
        "period": period,
        "kind": kind,
        "gstin": gstin or None,
        "counts": counts,
        "unmatchable_books": unmatchable,
        "itc": {
            "books": str(q(itc_books)),
            "portal": str(q(itc_portal)),
            "difference": str(q(itc_books - itc_portal)),
            "portal_ineligible_rows": len(portal_ineligible),
            "portal_ineligible_tax": str(q(ineligible_tax)),
        },
        "rows": [
            {
                "state": r.state,
                "supplier_gstin": r.supplier_gstin,
                "party": r.party,
                "doc_type": r.doc_type,
                "doc_no": r.doc_no,
                "doc_date": r.doc_date,
                "books_taxable": str(r.books_taxable) if r.books_taxable is not None else None,
                "books_tax": str(r.books_tax) if r.books_tax is not None else None,
                "portal_taxable": str(r.portal_taxable) if r.portal_taxable is not None else None,
                "portal_tax": str(r.portal_tax) if r.portal_tax is not None else None,
                "reason": r.reason,
            }
            for r in rows
        ],
    }


GST_RECON_CSV_HEADER = (
    "State",
    "GSTIN",
    "Party",
    "Doc Type",
    "Doc No",
    "Date",
    "Books Taxable",
    "Books Tax",
    "Portal Taxable",
    "Portal Tax",
    "Reason",
)


def gst_recon_csv(data: dict) -> str:
    rows = [
        [
            r["state"],
            r["supplier_gstin"],
            r["party"],
            r["doc_type"],
            r["doc_no"],
            r["doc_date"],
            r["books_taxable"] or "",
            r["books_tax"] or "",
            r["portal_taxable"] or "",
            r["portal_tax"] or "",
            r["reason"],
        ]
        for r in data["rows"]
    ]
    return _csv(GST_RECON_CSV_HEADER, rows)


def _recon_section(title: str, state: str, data: dict) -> DocumentSection:
    wanted = [r for r in data["rows"] if r["state"] == state]
    return DocumentSection(
        title=title,
        columns=(
            DocumentColumn("GSTIN", width=1.35),
            DocumentColumn("Party", width=1.3),
            DocumentColumn("Doc", width=0.9),
            DocumentColumn("Date", width=0.8),
            DocumentColumn("Books taxable", width=0.95, align="right"),
            DocumentColumn("Portal taxable", width=0.95, align="right"),
            DocumentColumn("Books tax", width=0.85, align="right"),
            DocumentColumn("Portal tax", width=0.85, align="right"),
            DocumentColumn("Reason", width=1.7),
        ),
        rows=tuple(
            (
                r["supplier_gstin"],
                r["party"],
                f"{r['doc_type']} {r['doc_no']}",
                r["doc_date"],
                format_amount(Decimal(r["books_taxable"])) if r["books_taxable"] else "—",
                format_amount(Decimal(r["portal_taxable"])) if r["portal_taxable"] else "—",
                format_amount(Decimal(r["books_tax"])) if r["books_tax"] else "—",
                format_amount(Decimal(r["portal_tax"])) if r["portal_tax"] else "—",
                r["reason"] or "—",
            )
            for r in wanted
        ),
    )


def gst_recon_view(data: dict) -> DocumentView:
    c = data["counts"]
    itc = data["itc"]
    sections = (
        _recon_section("Matched — key and amounts agree", "matched", data),
        _recon_section("Mismatched — the key agrees, the facts do not", "mismatched", data),
        _recon_section(
            "Missing in books — the portal drafted it, the books do not hold it",
            "missing_books",
            data,
        ),
        _recon_section(
            "Missing in portal — the books claim it, the portal has not drafted it",
            "missing_portal",
            data,
        ),
    )
    footnotes = [
        f"The GSTR-{data['kind']} comparison: the books' inward B2B register "
        "(purchases and debit notes, keyed on the supplier's GSTIN and their own "
        "document number) against the imported portal statement — matched, "
        "mismatched, missing both sides, every reason named.",
        "A books debit note pairs the supplier's credit note (or debit note): "
        "the same physical document named from both ends — books returns "
        "negate their amounts, the portal states face values; the comparison "
        "prices magnitudes.",
        f"ITC as the books claim it: {itc['books']}; as the portal states it: "
        f"{itc['portal']} — the difference {itc['difference']} is the month's "
        "reconciliation work, exactly priced.",
    ]
    if itc["portal_ineligible_rows"]:
        footnotes.append(
            f"{itc['portal_ineligible_rows']} portal row(s) are marked ITC-ineligible "
            f"({itc['portal_ineligible_tax']} of tax): the portal's own verdict — "
            "reverse the claim before the 3B's Table 4."
        )
    if data["unmatchable_books"]:
        footnotes.append(
            f"{data['unmatchable_books']} inward document(s) carry no supplier GSTIN "
            "or no supplier invoice number (meta.gst.invoice_no) and cannot be "
            "matched at all — honest blanks, never invented keys."
        )
    if not data["rows"]:
        footnotes.append(
            "Nothing to compare: no matched keys this period — import the portal "
            "statement and post the inward register first."
        )
    return DocumentView(
        title=f"GSTR-{data['kind']} reconciliation",
        subtitle=f"Books vs portal, the ITC both sides claim — {_period_label(data['period'])}",
        fields=(
            ("Period", _period_label(data["period"])),
            ("Statement", f"GSTR-{data['kind']}"),
            ("Registration", data["gstin"] or "all (pass ?gstin= to scope one)"),
            ("Matched / mismatched", f"{c['matched']} / {c['mismatched']}"),
            ("Missing in books / portal", f"{c['missing_books']} / {c['missing_portal']}"),
        ),
        sections=sections,
        totals=(
            ("ITC as the books claim", format_amount(Decimal(itc["books"]))),
            ("ITC as the portal states", format_amount(Decimal(itc["portal"]))),
            ("Difference (books − portal)", format_amount(Decimal(itc["difference"]))),
        ),
        footnotes=tuple(footnotes),
        filename=f"GSTR{data['kind']} recon {data['period']}",
    )


def gst_recon_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The GSTR-2A/2B ITC reconciliation — PDF (default), CSV or the JSON
    payload. Reads the imported portal statement through the versioned
    plugin API (0.13), the banking precedent."""
    period = _required_period(params)
    _format(params)  # validates pdf/csv/json — an unknown format never renders
    kind = _recon_kind(params)
    gstin = _gstin_filter(params)
    data = gst_reconciliation(resolver, period, kind, _quantizer(params), gstin)
    return PackReport(
        title=f"GSTR-{kind} reconciliation {period}",
        filename=f"GSTR{kind} recon {period}",
        view=gst_recon_view(data),
        csv=gst_recon_csv(data),
        data=data,
    )


# ---------------------------------------------- the e-way-bill outbox (ADR-028)
def ewb_envelope(event_data: dict) -> dict | None:
    """The movement envelope for one posted voucher, or ``None`` when the
    posting is not queueable: only document-mode vouchers carry goods
    facts (quick-tax carries none), and only those at or above the
    movement threshold need an e-way bill — ``LF_GST_EWB_THRESHOLD``
    (default ₹50,000, Rule 138's own line) moves the deployment's line.
    ``policy:skip_ewb`` opts a voucher out (a service document, an exempt
    lane, a courier the operator handles their own way)."""
    meta = event_data.get("meta") or {}
    doc = meta.get(META_KEY)
    if not isinstance(doc, dict):
        return None
    if meta.get("policy:skip_ewb"):
        return None
    computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
    mode = str(computed.get("mode") or "")
    if mode not in _MODE_SHAPES:
        return None  # quick-tax or foreign — no goods facts to carry
    try:
        grand_total = Decimal(str(computed.get("grand_total") or 0))
    except ArithmeticError:
        return None
    raw_threshold = os.environ.get("LF_GST_EWB_THRESHOLD", "").strip()
    try:
        threshold = Decimal(raw_threshold) if raw_threshold else EWB_THRESHOLD
    except ArithmeticError:
        threshold = EWB_THRESHOLD
    if grand_total < threshold:
        return None
    eway = doc.get("eway") if isinstance(doc.get("eway"), dict) else {}
    return {
        # the operator's adapter contract (ADR-028), not the government
        # schema — the NIC adapter translates to the portal's format
        "kind": "lf-gst-ewaybill/1",
        "queued_at": None,  # stamped by the queue writer
        "book_id": event_data.get("book_id"),
        "voucher_id": event_data.get("voucher_id"),
        "voucher_no": event_data.get("voucher_no"),
        "voucher_date": event_data.get("voucher_date"),
        "doc_type": DOC_TYPE_FOR_MODE[mode],
        "mode": mode,
        "registrant": {
            "state": computed.get("supplier_state", ""),
            "gstin": computed.get("supplier_gstin", ""),
        },
        "counterparty": {
            "gstin": computed.get("party_gstin", ""),
            "name": str(doc.get("party") or ""),
            "place_of_supply": computed.get("place_of_supply", ""),
        },
        "invoice_no": str(computed.get("invoice_no") or "") or event_data.get("voucher_no", ""),
        "movement": {  # the operator's Part-B facts, echoed for the adapter
            "transporter_gstin": str(eway.get("transporter_gstin") or ""),
            "transporter_name": str(eway.get("transporter_name") or ""),
            "transporter_doc_no": str(eway.get("transporter_doc_no") or ""),
            "transporter_doc_date": str(eway.get("transporter_doc_date") or ""),
            "vehicle_no": str(eway.get("vehicle_no") or ""),
            "from_place": str(eway.get("from_place") or ""),
            "distance_km": eway.get("distance_km"),
        },
        "totals": {
            "taxable": computed.get("subtotal", ""),
            "cgst": computed.get("cgst_total", ""),
            "sgst": computed.get("sgst_total", ""),
            "igst": computed.get("igst_total", ""),
            "grand_total": computed.get("grand_total", ""),
        },
    }


def queue_for_ewb(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    """The after-commit half of the e-way-bill bridge: append every
    threshold-crossing document's movement envelope to the
    ``LF_GST_EWB_OUTBOX`` JSONL file — the queue_for_irp discipline
    exactly (out-of-process by contract D3: no network I/O inside the
    posting path, and a missing/unwritable outbox is logged and isolated
    — an event handler must never break posting)."""
    from datetime import UTC, datetime

    outbox = os.environ.get("LF_GST_EWB_OUTBOX", "").strip()
    if not outbox:
        return
    envelope = ewb_envelope(event.data)
    if envelope is None:
        return
    envelope["queued_at"] = datetime.now(UTC).isoformat()
    path = Path(outbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    logger.info("queued %s for the e-way-bill adapter", event.data.get("voucher_no"))


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_gst_document)
    registry.register_validator(no_weekend_posting)
    registry.on("voucher.posted", record_posted)
    registry.on("voucher.posted", queue_for_irp)
    registry.on("voucher.posted", queue_for_ewb)
    registry.register_report(GSTR1_KEY, gstr1_report)
    registry.register_report(GST_HSN_KEY, hsn_report)
    registry.register_report(GSTR3B_KEY, gstr3b_report)
    registry.register_report(GSTR9_KEY, gstr9_report)
    registry.register_report(GST_RECON_KEY, gst_recon_report)


__all__ = [
    "DEFAULT_HSN_TABLE",
    "EWB_THRESHOLD",
    "GST_HSN_KEY",
    "GST_HSN_CSV_HEADER",
    "GST_RECON_KEY",
    "GST_RECON_CSV_HEADER",
    "GSTR1_KEY",
    "GSTR1_CSV_HEADER",
    "GSTR3B_KEY",
    "GSTR3B_CSV_HEADER",
    "GSTR9_KEY",
    "GSTR9_CSV_HEADER",
    "HsnRate",
    "HsnRow",
    "GstInvoice",
    "InwardDoc",
    "ReconRow",
    "META_KEY",
    "PLUGIN",
    "POSTED_EVENTS",
    "TAX_SLABS",
    "build_gst_document",
    "check_gstin",
    "gstr1_csv",
    "gstr1_invoices",
    "gstr1_report",
    "gstr1_view",
    "gstr3b_csv",
    "gstr3b_report",
    "gstr3b_summary",
    "gstr3b_view",
    "gstr9_csv",
    "gstr9_report",
    "gstr9_tables",
    "gstr9_view",
    "gst_reconciliation",
    "gst_recon_csv",
    "gst_recon_report",
    "gst_recon_view",
    "hsn_csv",
    "hsn_description",
    "hsn_report",
    "hsn_rows",
    "hsn_table",
    "hsn_view",
    "inward_documents",
    "irp_envelope",
    "load_hsn_table",
    "no_weekend_posting",
    "parse_state",
    "queue_for_ewb",
    "queue_for_irp",
    "rate_for",
    "register",
    "supply_kind",
]
