"""Example plugin: intra-state GST split + a weekend-posting policy.

Demonstrates every hook type (docs/plugin-guide.md):

- transformer: given ``meta: {"gst": {"rate": "18"}}`` on a sales-style
  voucher, computes tax on the first credit line, appends CGST/SGST credit
  lines under *Duties & Taxes*, and adds the tax to the first debit line —
  exactly the shape TallyPrime produces for a GST sale.
- validator: ``meta: {"policy:no_weekends": true}`` blocks weekend posting.
- event handler: records every ``voucher.posted`` event for observability.

All arithmetic is exact ``Decimal`` (Law 3); tax rounds half-up to 2 places,
and the SGST line is computed as the remainder so the split always sums
exactly to the total tax.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from ..errors import ValidationError
from ..plugins import DraftLine, PluginManifest, Resolver, VoucherDraft

logger = logging.getLogger("ledgerforge.plugins.example_gst")

PLUGIN = PluginManifest(name="example-gst", version="0.1.0", api_compat="0.1")

#: Every voucher posted while this plugin is loaded (observability for tests/demos).
POSTED_EVENTS: list[dict] = []


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(add_gst_lines)
    registry.register_validator(no_weekend_posting)
    registry.on("voucher.posted", record_posted)


def _tax_lines(draft: VoucherDraft, resolver: Resolver) -> list[DraftLine]:
    cfg = draft.meta.get("gst")
    if not cfg:
        return []
    rate = cfg.get("rate", 0)
    if Decimal(str(rate)) <= 0:
        return []
    credit_lines = [ln for ln in draft.lines if ln.dc == "C"]
    debit_lines = [ln for ln in draft.lines if ln.dc == "D"]
    if not credit_lines or not debit_lines:
        raise ValidationError("gst: voucher needs at least one debit and one credit line")
    base = credit_lines[0].amount
    tax = (base * Decimal(str(rate)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    cgst = (tax / Decimal(2)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sgst = tax - cgst  # remainder: CGST + SGST == tax, exactly
    cgst_ledger = resolver.ensure_ledger("CGST Payable", "Duties & Taxes")
    sgst_ledger = resolver.ensure_ledger("SGST Payable", "Duties & Taxes")
    return [
        DraftLine(dc="C", amount=cgst, ledger_id=cgst_ledger.id, narration="CGST"),
        DraftLine(dc="C", amount=sgst, ledger_id=sgst_ledger.id, narration="SGST"),
    ]


def add_gst_lines(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    lines = list(draft.lines)
    extras = _tax_lines(draft, resolver)
    if not extras:
        return draft
    tax_total = sum((ln.amount for ln in extras), Decimal(0))
    # add the tax to the first debit line (the party line, Tally-style)
    first_debit = next(ln for ln in lines if ln.dc == "D")
    lines[lines.index(first_debit)] = DraftLine(
        dc="D",
        amount=first_debit.amount + tax_total,
        ledger_id=first_debit.ledger_id,
        ledger_name=first_debit.ledger_name,
        narration=first_debit.narration,
    )
    return draft.with_lines(lines + extras)


def no_weekend_posting(draft: VoucherDraft, resolver: Resolver) -> None:
    if draft.meta.get("policy:no_weekends") and draft.voucher_date.weekday() >= 5:
        raise ValidationError(
            f"Policy: posting on {draft.voucher_date.isoformat()} is blocked "
            "(weekend; override with policy:no_weekends=false)"
        )


def record_posted(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    POSTED_EVENTS.append({"name": event.name, "voucher_no": event.data.get("voucher_no")})
    logger.info("voucher posted: %s", event.data.get("voucher_no"))
