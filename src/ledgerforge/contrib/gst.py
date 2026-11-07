"""The GST compliance pack (India) — place of supply, HSN rate tables,
GSTR-ready statements and the e-invoicing outbox (ADR-017, issue #6).

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
- **Document mode** ``meta.gst``: ``mode`` ``sale``/``purchase``, ``party``,
  ``party_gstin``, ``supplier_state``, ``place_of_supply`` and ``items``
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
  with taxable value and the CGST/SGST/IGST split — and ``GET /api/v1/
  reports/pack/gst-hsn?period=YYYY-MM`` — the HSN summary (per HSN × rate:
  quantity, taxable value, tax split). ``?format=csv`` serves the machine
  format; the default renders the PDF through the shared layout engine.
  A *reporting bridge*, not a portal-certified file generator: every
  statement carries the applied rules in its footer.
- **The e-invoicing (IRN) outbox**: every posted B2B sale (a document-mode
  sale carrying ``party_gstin``) is appended — after commit, out of the
  posting path — as an IRP-ready envelope to the JSONL outbox named by
  ``LF_GST_OUTBOX``. ``lf-irn-bridge`` (``python -m ledgerforge.contrib.
  irn_bridge``) ships the outbox to the operator's IRP adapter **in a
  separate process** and records the returned IRN/ack — latency-tolerant
  integration stays out-of-process by contract (D3), while the tax itself
  ran inside the posting transaction where Law 1 lives.

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

PLUGIN = PluginManifest(name="gst", version="0.1.0", api_compat="0.6")

GSTR1_KEY = "gstr1"
GST_HSN_KEY = "gst-hsn"
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
            raise ValidationError(
                f"gst: HSN table row {i}: hsn must be 2-8 digits, got {hsn!r}"
            )
        try:
            rate = Decimal(str(row.get("rate")))
        except ArithmeticError:
            raise ValidationError(
                f"gst: HSN table row {i}: rate must be a number"
            ) from None
        if rate < 0:
            raise ValidationError(f"gst: HSN table row {i}: rate must be >= 0")
        try:
            eff_from = date.fromisoformat(str(row.get("effective_from") or ""))
            eff_to = (
                date.fromisoformat(str(row["effective_to"]))
                if row.get("effective_to")
                else None
            )
        except ValueError:
            raise ValidationError(
                f"gst: HSN table row {i}: effective_from/effective_to must be YYYY-MM-DD"
            ) from None
        if eff_to is not None and eff_to < eff_from:
            raise ValidationError(
                f"gst: HSN table row {i}: effective_to precedes effective_from"
            )
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
        if digits.startswith(row.hsn) and row.effective_from <= on
        and (row.effective_to is None or on <= row.effective_to)
    ]
    if not candidates:
        return None
    latest = max(row.effective_from for row in candidates)
    winners = [row for row in candidates if row.effective_from == latest]
    rates = {row.rate for row in winners}
    if len(rates) > 1:
        pretty = ", ".join(
            sorted(f"{row.rate}% from {row.effective_from}" for row in winners)
        )
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
        if digits.startswith(row.hsn) and row.effective_from <= on
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
                f"gst: {where}.tax_rate {raw!r} is not a GST slab "
                f"(one of {', '.join(TAX_SLABS)})"
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
    raise ValidationError(
        f"gst: {where}: pass tax_rate (a slab) or hsn (a table-known chapter)"
    )


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


def _build_document_mode(
    draft: VoucherDraft, resolver: Resolver, doc: dict
) -> VoucherDraft:
    mode = str(doc.get("mode") or "sale").strip().lower()
    if mode not in ("sale", "purchase"):
        raise ValidationError(
            f"gst: mode must be one of sale, purchase, got {doc.get('mode')!r}"
        )
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
    supplier_state = _supplier_state(doc)
    pos_code, pos_name = (
        parse_state(doc["place_of_supply"], "gst.place_of_supply")
        if doc.get("place_of_supply")
        else (supplier_state, STATE_CODES[supplier_state])
    )
    kind = supply_kind(supplier_state, pos_code)

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

        if mode == "sale":
            ledger = resolver.ensure_ledger(
                str(item.get("revenue_ledger") or DEFAULT_REVENUE_LEDGER), REVENUE_GROUP
            )
            lines.append(
                DraftLine(
                    dc="C",
                    amount=amount,
                    ledger_id=ledger.id,
                    ledger_name=ledger.name,
                    narration=f"{description} × {qty} @ {rate}",
                )
            )
        else:
            ledger = resolver.ensure_ledger(
                str(item.get("purchase_ledger") or DEFAULT_PURCHASE_LEDGER), PURCHASE_GROUP
            )
            lines.append(
                DraftLine(
                    dc="D",
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
                # Input CGST/SGST/IGST inward — all under Duties & Taxes
                ledger_name = (
                    f"{base_name} Payable" if mode == "sale" else f"Input {base_name}"
                )
                tax_ledger = resolver.ensure_ledger(ledger_name, TAX_GROUP)
                lines.append(
                    DraftLine(
                        dc="C" if mode == "sale" else "D",
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
    party_group = DEBTOR_GROUP if mode == "sale" else CREDITOR_GROUP
    party_ledger = resolver.ensure_ledger(party, party_group)
    narration = f"{'sale to' if mode == 'sale' else 'purchase from'} {party}"
    if gstin:
        narration += f" ({gstin})"

    lines.insert(
        0,
        DraftLine(
            dc="D" if mode == "sale" else "C",
            amount=grand_total,
            ledger_id=party_ledger.id,
            ledger_name=party_ledger.name,
            narration=narration,
        ),
    )

    doc["computed"] = {
        "mode": mode,
        "supply": kind,
        "supplier_state": supplier_state,
        "place_of_supply": pos_code,
        "place_of_supply_name": pos_name,
        "party_gstin": gstin,
        "subtotal": str(subtotal),
        "cgst_total": str(cgst_total),
        "sgst_total": str(sgst_total),
        "igst_total": str(igst_total),
        "tax_total": str(tax_total),
        "grand_total": str(grand_total),
    }
    return draft.with_lines(lines)


def _build_quick_tax(
    draft: VoucherDraft, resolver: Resolver, doc: dict
) -> VoucherDraft:
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
    for item in (doc.get("items") or []):
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
    """One outward invoice's GSTR-1 row — the posted facts, replayed."""

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


def _net_gst_documents(resolver: Resolver) -> list[PriorDocument]:
    """The document-store fold's shared discipline (ADR-011/012/014): every
    ``meta.gst`` voucher, oldest first, with whole-voucher reversals netted
    out (a reversed original drops, exactly like the payroll fold)."""
    documents = resolver.prior_documents(META_KEY)
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    return [
        d
        for d in documents
        if d.reversal_of is None and d.voucher_id not in reversed_ids
    ]


def _in_period(document: PriorDocument, period: str) -> bool:
    return document.voucher_date.isoformat()[:7] == period


def gstr1_invoices(
    resolver: Resolver,
    period: str,
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
) -> tuple[list[GstInvoice], int]:
    """The outward B2B register of one ``YYYY-MM``: every document-mode sale
    carrying a GSTIN (B2B), amounts replayed from the posted items and the
    immutable ``computed`` block — the same figures the ledger posted, by
    construction. Returns the rows plus the count of quick-tax vouchers the
    statement deliberately excludes (they carry no HSN/party facts — the
    register cannot invent them)."""
    q = quantize or _money
    invoices: list[GstInvoice] = []
    quick_tax = 0
    for d in _net_gst_documents(resolver):
        if not _in_period(d, period):
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
        mode = computed.get("mode") or doc.get("mode")
        if mode == "purchase":
            continue
        if not doc.get("items"):
            if mode == "sale" or doc.get("rate") is not None:
                quick_tax += 1
            continue
        gstin = str(computed.get("party_gstin") or doc.get("party_gstin") or "").strip()
        if not gstin:
            continue  # B2C: the register is B2B — counted in the summary, not listed
        kind = str(computed.get("supply") or "intra")
        taxable = Decimal("0.00")
        cgst = Decimal("0.00")
        sgst = Decimal("0.00")
        igst = Decimal("0.00")
        rates: list[str] = []
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
            label = f"{slab.normalize():f}"
            if label not in rates:
                rates.append(label)
        invoices.append(
            GstInvoice(
                gstin=gstin,
                party=str(doc.get("party") or ""),
                voucher_no=d.voucher_no,
                voucher_date=d.voucher_date,
                place_of_supply=str(computed.get("place_of_supply") or ""),
                taxable=q(taxable),
                cgst=q(cgst),
                sgst=q(sgst),
                igst=q(igst),
                rates=tuple(rates),
            )
        )
    invoices.sort(key=lambda inv: (inv.voucher_date, inv.voucher_no))
    return invoices, quick_tax


def hsn_rows(
    resolver: Resolver,
    period: str,
    side: str = "outward",
    quantize=None,  # noqa: ANN001 — Callable[[Decimal], Decimal] | None
) -> list[HsnRow]:
    """The HSN summary of one ``YYYY-MM``: per (HSN, rate) — quantity,
    taxable value and the tax split, outward and/or inward (``side=``).
    Quick-tax vouchers are excluded: no HSN, no summary row — a footnote
    says so, honestly."""
    q = quantize or _money
    wanted = {"outward": ("sale",), "inward": ("purchase",)}.get(side, ("sale", "purchase"))
    rows: dict[tuple[str, str, str], HsnRow] = {}
    for d in _net_gst_documents(resolver):
        if not _in_period(d, period):
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
        mode = computed.get("mode") or doc.get("mode")
        if mode not in wanted:
            continue
        kind = str(computed.get("supply") or "intra")
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
            direction = "outward" if mode == "sale" else "inward"
            amount = _money(qty * rate)
            tax = _money(amount * slab / _HUNDRED)
            ic, isg, iig = _tax_split(tax, kind)
            key = (direction, hsn, f"{slab.normalize():f}")
            row = rows.get(key)
            if row is None:
                rows[key] = HsnRow(
                    direction=direction,
                    hsn=hsn,
                    description=description,
                    rate=slab,
                    qty=qty,
                    taxable=amount,
                    cgst=ic,
                    sgst=isg,
                    igst=iig,
                )
            else:
                rows[key] = HsnRow(
                    direction=row.direction,
                    hsn=row.hsn,
                    description=row.description or description,
                    rate=row.rate,
                    qty=row.qty + qty,
                    taxable=row.taxable + amount,
                    cgst=row.cgst + ic,
                    sgst=row.sgst + isg,
                    igst=row.igst + iig,
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
        raise ValidationError(
            "gst: pass ?period=YYYY-MM — the tax month the statement covers"
        )
    year, month = period[:4], period[5:7]
    if (
        len(period) != 7
        or not year.isdigit()
        or not month.isdigit()
        or not 1 <= int(month) <= 12
    ):
        raise ValidationError(f"gst: period must be YYYY-MM, got {period!r}")
    return period


def _format(params: dict[str, str]) -> str:
    fmt = str(params.get("format") or "pdf").strip().lower()
    if fmt not in ("pdf", "csv"):
        raise ValidationError(f"gst: format must be one of pdf, csv, got {fmt!r}")
    return fmt


def _side(params: dict[str, str]) -> str:
    side = str(params.get("side") or "outward").strip().lower()
    if side not in ("outward", "inward", "both"):
        raise ValidationError(
            f"gst: side must be one of outward, inward, both, got {side!r}"
        )
    return side


def _quantizer(params: dict[str, str]):
    mode = str(params.get("round") or "cent").strip().lower()
    if mode == "cent":
        return _money
    if mode == "rupee":
        return lambda value: value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    raise ValidationError(f"gst: round must be one of cent, rupee, got {mode!r}")


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _period_label(period: str) -> str:
    try:
        return f"{_MONTHS[int(period[5:7]) - 1]} {period[:4]}"
    except (ValueError, IndexError):
        return period


# ------------------------------------------------------------------- CSV
GSTR1_CSV_HEADER = (
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


def gstr1_view(period: str, invoices: list[GstInvoice], quick_tax: int) -> DocumentView:
    total = lambda pick: sum((pick(i) for i in invoices), Decimal("0.00"))  # noqa: E731
    rows = [
        (
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
    footnotes = [
        "The outward B2B register: document-mode sales carrying a GSTIN, folded from "
        "posted meta.gst vouchers with reversals netted (Law 2).",
        f"Supplier state: {_supplier_label()}.",
        "Quick-tax vouchers (meta.gst.rate) carry no HSN/party facts and are not "
        "in this register — a blank is honest, an invented GSTIN is not.",
        "GSTINs are format-checked, checksums unverified — verify against the "
        "portal's current template (GSTR-1) before uploading.",
    ]
    if quick_tax:
        footnotes.insert(
            2,
            f"{quick_tax} quick-tax voucher(s) posted this period are excluded (no "
            "HSN/party facts).",
        )
    if not invoices:
        footnotes.append("No B2B sales posted for this period — a nil statement.")

    return DocumentView(
        title="GSTR-1 — outward supplies",
        subtitle=f"B2B invoice register — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("B2B invoices", str(len(invoices))),
            ("Register", "B2B (GSTIN-wise)"),
        ),
        sections=(
            DocumentSection(
                title="B2B invoices",
                columns=(
                    DocumentColumn("GSTIN", width=1.35),
                    DocumentColumn("Party", width=1.35),
                    DocumentColumn("Invoice", width=1.05),
                    DocumentColumn("Date", width=0.8),
                    DocumentColumn("POS", width=0.45),
                    DocumentColumn("Rate", width=0.5, align="right"),
                    DocumentColumn("Taxable", width=0.95, align="right"),
                    DocumentColumn("CGST", width=0.8, align="right"),
                    DocumentColumn("SGST", width=0.8, align="right"),
                    DocumentColumn("IGST", width=0.8, align="right"),
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
    """The GSTR-1 outward B2B register of one tax month, PDF (default) or CSV."""
    period = _required_period(params)
    invoices, quick_tax = gstr1_invoices(resolver, period, _quantizer(params))
    if _format(params) == "csv":
        return PackReport(
            title=f"GSTR-1 {period}", filename=f"GSTR1 {period}", csv=gstr1_csv(invoices)
        )
    return PackReport(
        title=f"GSTR-1 {period}",
        filename=f"GSTR1 {period}",
        view=gstr1_view(period, invoices, quick_tax),
    )


def hsn_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The HSN summary of one tax month, PDF (default) or CSV."""
    period = _required_period(params)
    side = _side(params)
    rows = hsn_rows(resolver, period, side, _quantizer(params))
    if _format(params) == "csv":
        return PackReport(
            title=f"GST HSN summary {period}",
            filename=f"GST HSN summary {period}",
            csv=hsn_csv(rows),
        )
    return PackReport(
        title=f"GST HSN summary {period}",
        filename=f"GST HSN summary {period}",
        view=hsn_view(period, rows, side),
    )


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_gst_document)
    registry.register_validator(no_weekend_posting)
    registry.on("voucher.posted", record_posted)
    registry.on("voucher.posted", queue_for_irp)
    registry.register_report(GSTR1_KEY, gstr1_report)
    registry.register_report(GST_HSN_KEY, hsn_report)


__all__ = [
    "DEFAULT_HSN_TABLE",
    "GST_HSN_KEY",
    "GST_HSN_CSV_HEADER",
    "GSTR1_KEY",
    "GSTR1_CSV_HEADER",
    "HsnRate",
    "HsnRow",
    "GstInvoice",
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
    "hsn_csv",
    "hsn_description",
    "hsn_report",
    "hsn_rows",
    "hsn_table",
    "hsn_view",
    "irp_envelope",
    "load_hsn_table",
    "no_weekend_posting",
    "parse_state",
    "queue_for_irp",
    "rate_for",
    "register",
    "supply_kind",
]
