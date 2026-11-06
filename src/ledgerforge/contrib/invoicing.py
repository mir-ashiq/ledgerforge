"""Invoicing plugin pack: the sales invoice and the AR receipt as documents.

The roadmap's promise (analysis §9) is that invoicing lives ON the kernel,
not IN it. This pack is that promise kept — the first complete business
document built purely on the plugin runtime (ADR-003):

- transformer ``build_invoice_document``: ``meta.invoice`` carries the
  commercial document (customer, items with qty/rate/tax_rate, due_days).
  The transformer derives the exact accounting shape TallyPrime produces
  for a GST sale — one debit to the party ledger for the grand total, one
  credit line per item on its revenue ledger (narration keeps
  ``description × qty @ rate``), and CGST/SGST split lines per taxed item —
  then annotates the computed totals into ``meta.invoice.computed``. With
  ADR-010 the document persists with the voucher: ``GET /vouchers/{id}``
  returns the complete invoice, not just its accounting shadow.
- transformer ``build_receipt_document``: ``meta.receipt`` settles AR —
  debit the bank ledger, credit the customer.
- validator ``no_future_document_dates``: documents dated in the future are
  fat-finger candidates; vetoed unless the draft carries
  ``meta["policy:allow_future_document_dates"] = true``.
- event handler ``record_issued``: every posted invoice/receipt lands in
  ``ISSUED`` — the observability seam an e-invoicing bridge or BI sync
  would attach to.

All arithmetic is exact ``Decimal`` (Law 3). Tax rounds half-up to 2
places and the SGST line is the remainder, so CGST + SGST == tax exactly
(same split discipline as the example GST pack). In document mode the
``entries`` the client sends are placeholders (the API schema requires at
least one); the document is the source of truth and the transformer
replaces them.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from ..errors import ValidationError
from ..plugins import DraftLine, PluginManifest, Resolver, VoucherDraft

logger = logging.getLogger("ledgerforge.plugins.invoicing")

PLUGIN = PluginManifest(name="invoicing", version="0.1.0", api_compat="0.2")

#: GST slabs — the whitelist keeps a fat-fingered rate (15, 20, …) from
#: silently posting the wrong tax. Zero is a valid (exempt) rate.
TAX_SLABS = ("0", "5", "12", "18", "28")

# Seed groups (ledgerforge/seeding.py) — the pack resolves/creates masters
# only through Resolver, so everything stays book-scoped (ADR-009).
PARTY_GROUP = "Sundry Debtors"  # AR customers, Current Assets
REVENUE_GROUP = "Sales Accounts"  # income
TAX_GROUP = "Duties & Taxes"  # CGST/SGST payable
BANK_GROUP = "Bank Accounts"  # receipt settlement
DEFAULT_REVENUE_LEDGER = "Sales"
DEFAULT_BANK_LEDGER = "Bank"

#: Every invoice/receipt posted while this pack is loaded (observability
#: for tests/demos; the real-world consumer is an e-invoicing bridge).
ISSUED: list[dict] = []

CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_TWO = Decimal(2)


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_invoice_document)
    registry.register_transformer(build_receipt_document)
    registry.register_validator(no_future_document_dates)
    registry.on("voucher.posted", record_issued)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"invoicing: {field} must be a number, got {value!r}") from None


def _reject_conflicts(draft: VoucherDraft, mode: str) -> None:
    other = "receipt" if mode == "invoice" else "invoice"
    if draft.meta.get(other) is not None:
        raise ValidationError(f"invoicing: meta.{mode} and meta.{other} are mutually exclusive")
    if draft.meta.get("gst") is not None:
        raise ValidationError(
            "invoicing: meta.gst would double-tax — this pack computes its own tax lines"
        )
    if draft.meta.get("stock") is not None:
        # Cross-pack exclusivity (ADR-011): the inventory pack derives lines
        # from meta.stock — two document modes in one draft is ambiguous
        # regardless of transformer order.
        raise ValidationError(
            "invoicing: meta.invoice/receipt and meta.stock are mutually exclusive "
            "(post the commercial and the stock documents separately)"
        )


def _require_customer(doc: dict, mode: str) -> str:
    customer = str(doc.get("customer") or "").strip()
    if not customer:
        raise ValidationError(f"invoicing: {mode}.customer is required")
    return customer


# --------------------------------------------------------- transformers
def build_invoice_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    inv = draft.meta.get("invoice")
    if inv is None:
        return draft
    _reject_conflicts(draft, "invoice")

    customer = _require_customer(inv, "invoice")
    items = inv.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("invoicing: invoice.items must be a non-empty list")

    party = resolver.ensure_ledger(customer, PARTY_GROUP)
    lines: list[DraftLine] = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")

    for i, item in enumerate(items, start=1):
        description = str(item.get("description") or "").strip() or f"item {i}"
        qty = _decimal(item.get("qty", 1), f"invoice.items[{i}].qty")
        rate = _decimal(item.get("rate", 0), f"invoice.items[{i}].rate")
        if qty <= 0:
            raise ValidationError(f"invoicing: invoice.items[{i}].qty must be > 0")
        if rate < 0:
            raise ValidationError(f"invoicing: invoice.items[{i}].rate must be >= 0")
        amount = _money(qty * rate)

        tax_rate_raw = str(item.get("tax_rate", "0"))
        if tax_rate_raw not in TAX_SLABS:
            raise ValidationError(
                f"invoicing: invoice.items[{i}].tax_rate {tax_rate_raw!r} is not a GST "
                f"slab (one of {', '.join(TAX_SLABS)})"
            )
        tax_rate = Decimal(tax_rate_raw)

        revenue = resolver.ensure_ledger(
            str(item.get("revenue_ledger") or DEFAULT_REVENUE_LEDGER), REVENUE_GROUP
        )
        lines.append(
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=revenue.id,
                ledger_name=revenue.name,
                narration=f"{description} × {qty} @ {rate}",
            )
        )
        subtotal += amount

        if tax_rate > 0:
            tax = _money(amount * tax_rate / _HUNDRED)
            cgst = _money(tax / _TWO)
            sgst = tax - cgst  # remainder: CGST + SGST == tax, exactly
            cgst_ledger = resolver.ensure_ledger("CGST Payable", TAX_GROUP)
            sgst_ledger = resolver.ensure_ledger("SGST Payable", TAX_GROUP)
            half = tax_rate / _TWO
            lines.append(
                DraftLine(
                    dc="C",
                    amount=cgst,
                    ledger_id=cgst_ledger.id,
                    ledger_name=cgst_ledger.name,
                    narration=f"CGST {half}% on {description}",
                )
            )
            lines.append(
                DraftLine(
                    dc="C",
                    amount=sgst,
                    ledger_id=sgst_ledger.id,
                    ledger_name=sgst_ledger.name,
                    narration=f"SGST {half}% on {description}",
                )
            )
            tax_total += tax

    grand_total = subtotal + tax_total

    due_note = ""
    computed: dict[str, str] = {
        "subtotal": str(subtotal),
        "tax_total": str(tax_total),
        "grand_total": str(grand_total),
    }
    due_days = inv.get("due_days")
    if due_days is not None:
        try:
            due_days_i = int(due_days)
        except (TypeError, ValueError):
            raise ValidationError("invoicing: invoice.due_days must be an integer") from None
        if due_days_i < 0:
            raise ValidationError("invoicing: invoice.due_days must be >= 0")
        due_date = draft.voucher_date + timedelta(days=due_days_i)
        computed["due_date"] = due_date.isoformat()
        due_note = f"; due {due_date.isoformat()}"

    lines.insert(
        0,
        DraftLine(
            dc="D",
            amount=grand_total,
            ledger_id=party.id,
            ledger_name=party.name,
            narration=f"invoice to {customer}{due_note}",
        ),
    )
    inv["computed"] = computed
    return draft.with_lines(lines)


def build_receipt_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    rec = draft.meta.get("receipt")
    if rec is None:
        return draft
    _reject_conflicts(draft, "receipt")

    customer = _require_customer(rec, "receipt")
    amount = _money(_decimal(rec.get("amount"), "receipt.amount"))
    if amount <= 0:
        raise ValidationError("invoicing: receipt.amount must be > 0")

    party = resolver.ensure_ledger(customer, PARTY_GROUP)
    bank = resolver.ensure_ledger(str(rec.get("bank_ledger") or DEFAULT_BANK_LEDGER), BANK_GROUP)

    reference = str(rec.get("reference") or "").strip()
    narration = f"receipt from {customer}" + (f"; {reference}" if reference else "")
    rec["computed"] = {"settled": str(amount)}
    return draft.with_lines(
        [
            DraftLine(
                dc="D",
                amount=amount,
                ledger_id=bank.id,
                ledger_name=bank.name,
                narration=narration,
            ),
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=party.id,
                ledger_name=party.name,
                narration=narration,
            ),
        ]
    )


# ------------------------------------------------------------ validators
def no_future_document_dates(draft: VoucherDraft, resolver: Resolver) -> None:
    if draft.meta.get("policy:allow_future_document_dates"):
        return
    mode = (
        "invoice"
        if draft.meta.get("invoice") is not None
        else ("receipt" if draft.meta.get("receipt") is not None else None)
    )
    if mode is None:
        return
    if draft.voucher_date > date.today():
        raise ValidationError(
            f"Policy: {draft.voucher_date.isoformat()} is in the future — {mode} documents "
            "post on or before today (override with policy:allow_future_document_dates=true)"
        )


# --------------------------------------------------------- event handler
def record_issued(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    meta = event.data.get("meta") or {}
    for kind in ("invoice", "receipt"):
        doc = meta.get(kind)
        if doc is None:
            continue
        ISSUED.append(
            {
                "kind": kind,
                "voucher_no": event.data.get("voucher_no"),
                "book_id": event.data.get("book_id"),
                "customer": (doc or {}).get("customer"),
            }
        )
        logger.info("%s posted: %s", kind, event.data.get("voucher_no"))
