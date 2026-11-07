"""Bill-wise & outstanding management (ADR-023, roadmap Phase 2).

TallyPrime's party ledgers carry **bill-wise details**: every invoice opens a
named bill, every receipt settles specific bills, and the receivables /
payables position is bill-level, not ledger-level. This pack is that, on the
kernel, with **zero schema change** — the third read-heavy pack to prove the
ADR-011 pattern (documents on ``vouchers.meta``, state as a deterministic
fold).

One document key, ``meta.bills``, two roles:

- **Allocations** (the annotation layer — never a line-builder). A voucher
  that moves a party ledger carries refs; every ref is a dr/cr allocation
  exactly like a ledger line (TallyPrime's bill-wise screen has Dr/Cr
  columns too):

    ``{"kind": "new", "ref": "INV-42", "dr": "1430.00", "due_days": 30}``
    — opens a receivable bill (``cr`` would open an advance);

    ``{"kind": "against", "ref": "INV-42", "cr": "500.00"}``
    — settles the named open bill toward zero (``dr`` settles a payable /
    advance);

    ``{"kind": "on_account", "cr": "100.00"}``
    — the party's on-account pool, surfaced everywhere, never auto-applied.

  The law the validator enforces (after ALL transformers, so document-mode
  lines are final — this pack never builds lines, so it never depends on
  transformer order):

    **Σ ref dr − Σ ref cr == the party's net movement on the voucher**

  Both sides start at zero, so by induction: **Σ open bill outstandings +
  on-account == the party's ledger balance**, at every point in the book's
  history. That master theorem is what the tests assert against the trial
  balance. Settlement is strictly toward zero — an ``against`` ref either
  reduces the bill's outstanding or closes it exactly; over-settlement is
  refused because it is always expressible correctly as against + on_account
  (the TallyPrime flow), which keeps the fold a trivial, policy-free replay.

- **``interest_charge``** (a document mode — the one line-builder here): the
  interest computation on overdue open bills, posted as a real voucher — one
  Dr party line per interest-bearing bill (narration carries the
  arithmetic), one Cr *Interest Income* line, and one auto-opened bill for
  the total (the charge must appear in the outstanding position or the
  master theorem breaks). Interest never posts as a side effect of anything:
  the ``bills-interest`` report previews the same fold, the voucher is
  reviewed and reversed like any other.

Rate resolution (a fold over declarations, newest by date): the bill's own
``interest`` → the party's ``default_interest`` (latest declared on or
before the bill's opening) → the request's explicit ``rate``/``grace_days``
→ none (the bill prints without interest, and a footnote says why — a blank
is honest, an invented rate is not).

Registered reports (ADR-014; PDF through the print bridge, CSV for
machines, JSON for UIs — plugin API 0.8):

- ``bills-receivable`` / ``bills-payable`` — open bills with ageing buckets
  (not due / 0-30 / 31-60 / 61-90 / 90+ days past due), on-account separate;
- ``ledger-statement`` — the party's running statement, opening balance to
  date, bill-linked through ``Resolver.ledger_entries`` (API 0.8);
- ``bills-interest`` — the overdue-interest computation (the preview);
- ``bills-open`` — one party's open bills + on-account (the dialog feed).

Reversal semantics (ADR-010/011): a reversal carries empty meta and rides
alongside its original in ``prior_documents``; the fold skips the undone
pair — undoing an invoice re-opens what it opened, undoing a receipt
restores what it settled, mirroring the mirrored entries.

All money is exact ``Decimal`` (Law 3); refs parse at 2 dp, interest
divides at 50-digit precision (the engine's headroom) and quantizes to
cents once at the end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, getcontext

from ..errors import NotFoundError, ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentSection,
    DocumentView,
    DraftLine,
    LedgerEntryLine,
    PackReport,
    PluginManifest,
    PriorDocument,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.bills")

# Exact-arithmetic headroom — same discipline as the posting engine (Law 3);
# the per-diem interest division runs at full precision wherever it runs.
getcontext().prec = 50

PLUGIN = PluginManifest(name="bills", version="0.1.0", api_compat="0.8")

META_KEY = "bills"

# Seed groups (ledgerforge/seeding.py) — the pack resolves the *income* side
# of an interest charge; parties are never invented, only found by name.
INTEREST_INCOME_GROUP = "Indirect Incomes"
INTEREST_INCOME_LEDGER = "Interest Income"
#: The groups whose ledgers are bill-managed parties — the web and native
#: dialogs key on these; the pack itself is group-agnostic (the party is
#: wherever its refs say).
PARTY_GROUPS = ("Sundry Debtors", "Sundry Creditors")

#: Every allocation/charge document posted while this pack is loaded
#: (observability for tests/demos; the real-world consumer is a dunning
#: or collection bridge).
POSTED: list[dict] = []

CENT = Decimal("0.01")
_HUNDRED = Decimal(100)
_YEAR_DAYS = Decimal(365)

REF_KINDS = ("new", "against", "on_account")
AGEING_BUCKETS = ("not due", "0-30", "31-60", "61-90", "90+")


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_interest_charge)
    registry.register_validator(validate_allocations)
    registry.on("voucher.posted", record_posted)
    registry.register_report("bills-receivable", receivable_report)
    registry.register_report("bills-payable", payable_report)
    registry.register_report("ledger-statement", statement_report)
    registry.register_report("bills-interest", interest_report)
    registry.register_report("bills-open", open_bills_report)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _amount(value: object, where: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"bills: {where} must be a number, got {value!r}") from None
    if not parsed.is_finite():
        raise ValidationError(f"bills: {where} must be a finite number")
    return _money(parsed)


def _ref_amount(ref: dict, where: str) -> tuple[Decimal, Decimal]:
    """A ref's dr/cr pair: exactly one of the two, both > 0."""
    dr = ref.get("dr")
    cr = ref.get("cr")
    if (dr is None) == (cr is None):
        raise ValidationError(
            f"bills: {where} needs exactly one of dr / cr (TallyPrime's bill-wise columns)"
        )
    side, raw = ("dr", dr) if cr is None else ("cr", cr)
    amount = _amount(raw, f"{where}.{side}")
    if amount <= 0:
        raise ValidationError(f"bills: {where}.{side} must be > 0")
    return (amount, Decimal("0.00")) if side == "dr" else (Decimal("0.00"), amount)


def _clean_ref_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValidationError("bills: refs with kind new/against need a non-empty ref")
    return name


def _due_date(ref: dict, voucher_date: date, where: str) -> date:
    """The bill's due date: an explicit due_on, or due_days from the voucher
    date, or the voucher date itself (due on demand)."""
    due_on = ref.get("due_on")
    if due_on is not None:
        try:
            return date.fromisoformat(str(due_on))
        except ValueError:
            raise ValidationError(f"bills: {where}.due_on must be YYYY-MM-DD") from None
    due_days = ref.get("due_days")
    if due_days is None:
        return voucher_date
    try:
        days = int(due_days)
    except (TypeError, ValueError):
        raise ValidationError(f"bills: {where}.due_days must be an integer") from None
    if days < 0:
        raise ValidationError(f"bills: {where}.due_days must be >= 0")
    return voucher_date + timedelta(days=days)


def _interest_cfg(raw: object, where: str) -> tuple[Decimal, int]:
    """(annual percent, grace days) from an interest declaration."""
    if not isinstance(raw, dict):
        raise ValidationError(f"bills: {where} must be an object")
    rate = _amount(raw.get("annual", 0), f"{where}.annual")
    if rate < 0:
        raise ValidationError(f"bills: {where}.annual must be >= 0")
    grace_raw = raw.get("grace_days", 0)
    try:
        grace = int(grace_raw)
    except (TypeError, ValueError):
        raise ValidationError(f"bills: {where}.grace_days must be an integer") from None
    if grace < 0:
        raise ValidationError(f"bills: {where}.grace_days must be >= 0")
    return rate, grace


# ------------------------------------------------------------------ the fold
@dataclass
class Bill:
    """One named bill of one party — derived state, never stored. A positive
    outstanding is a receivable; negative is a payable-side balance (an
    advance, or a supplier bill)."""

    party: str
    ref: str
    opened_on: date
    due_on: date
    amount_opened: Decimal  # signed at opening (dr > 0)
    outstanding: Decimal  # signed; 0 == closed
    interest_annual: Decimal | None = None  # percent p.a., from the bill's own declaration
    grace_days: int = 0
    settled_by: list[tuple[str, date]] = field(default_factory=list)  # (voucher_no, date)

    @property
    def side(self) -> str:
        return "dr" if self.outstanding > 0 else "cr"


@dataclass
class PartyState:
    """The fold's whole answer for one party: bills (closed ones kept for
    history and ref-uniqueness, excluded from reports) and the on-account
    pool, plus the latest declared default interest."""

    party: str
    bills: dict[str, Bill] = field(default_factory=dict)
    on_account: Decimal = Decimal("0.00")  # > 0: the party owes the pool; < 0: credit
    default_interest: tuple[Decimal, int] | None = None
    default_interest_on: date | None = None

    def open_bills(self) -> list[Bill]:
        return [b for b in self.bills.values() if b.outstanding != 0]


def _apply_ref(state: PartyState, ref: dict, voucher_no: str, voucher_date: date) -> None:
    """The single source of truth for bill arithmetic — used by the
    validator (guards live) and by every later fold (what posted is valid,
    so the same guards pass on replay). Structural errors fire identically
    in both."""
    kind = ref.get("kind")
    where = f"ref {ref.get('ref', kind)}"
    if kind == "new":
        name = _clean_ref_name(ref.get("ref"))
        dr, cr = _ref_amount(ref, where)
        amount = dr - cr
        existing = state.bills.get(name)
        if existing is not None and existing.outstanding != 0:
            raise ValidationError(
                f"bills: {where} collides with {state.party}'s open bill {name!r} — "
                "settle it first or use a distinct ref"
            )
        rate, grace = (None, 0)
        if ref.get("interest") is not None:
            rate, grace = _interest_cfg(ref["interest"], f"{where}.interest")
            if rate == 0:
                rate = None
        state.bills[name] = Bill(
            party=state.party,
            ref=name,
            opened_on=voucher_date,
            due_on=_due_date(ref, voucher_date, where),
            amount_opened=amount,
            outstanding=amount,
            interest_annual=rate,
            grace_days=grace,
        )
    elif kind == "against":
        name = _clean_ref_name(ref.get("ref"))
        dr, cr = _ref_amount(ref, where)
        delta = dr - cr
        bill = state.bills.get(name)
        if bill is None:
            raise ValidationError(
                f"bills: {where} settles a bill no prior document opened for {state.party!r}"
            )
        if bill.outstanding == 0:
            raise ValidationError(
                f"bills: {where} settles {state.party}'s bill {name!r} which is "
                "already fully settled"
            )
        new_outstanding = bill.outstanding + delta
        if new_outstanding == 0:
            pass  # closes exactly — the happy path
        elif (new_outstanding > 0) != (bill.outstanding > 0):
            # settling past zero flips the bill's side; the excess belongs
            # in on_account (the TallyPrime flow), so this is refused
            raise ValidationError(
                f"bills: {where} over-settles {state.party}'s bill {name!r}: "
                f"outstanding {format_amount(bill.outstanding)} cannot absorb "
                f"{format_amount(delta)} — allocate the excess as on_account"
            )
        elif abs(new_outstanding) > abs(bill.outstanding):
            # same sign but growing: not a settlement at all
            raise ValidationError(
                f"bills: {where} moves {state.party}'s bill {name!r} away from "
                "zero — against refs settle, they never add (open a new ref)"
            )
        bill.outstanding = new_outstanding
        bill.settled_by.append((voucher_no, voucher_date))
    elif kind == "on_account":
        dr, cr = _ref_amount(ref, where)
        state.on_account += dr - cr
    else:
        raise ValidationError(f"bills: ref.kind must be one of {', '.join(REF_KINDS)}")


def _declared_default_interest(doc: dict) -> tuple[Decimal, int] | None:
    raw = doc.get("default_interest")
    if raw is None:
        return None
    rate, grace = _interest_cfg(raw, "default_interest")
    return (rate, grace) if rate > 0 else None


def _sorted_documents(documents: list[PriorDocument]) -> list[PriorDocument]:
    """Oldest first by business date, voucher id breaking same-day ties —
    the order a deterministic replay needs (``prior_documents`` returns id
    order; a backdated bill must still fold after the bills it settles)."""
    return sorted(documents, key=lambda d: (d.voucher_date, d.voucher_id))


def _netted(documents: list[PriorDocument]) -> list[PriorDocument]:
    """Documents undone by a reversal net to zero: the reversal marks its
    original, and both are skipped (ADR-010/011)."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    return [d for d in documents if d.reversal_of is None and d.voucher_id not in reversed_ids]


def fold(documents: list[PriorDocument], *, as_of: date | None = None) -> dict[str, PartyState]:
    """Replay every posted bills document of the book, oldest first.
    ``as_of`` windows the replay to documents dated on or before it — the
    outstanding position as of that date, not today's."""
    states: dict[str, PartyState] = {}

    def state_for(party: str) -> PartyState:
        if party not in states:
            states[party] = PartyState(party=party)
        return states[party]

    for d in _netted(_sorted_documents(documents)):
        if as_of is not None and d.voucher_date > as_of:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        party = str(doc.get("party") or "").strip()
        if not party:
            continue
        state = state_for(party)
        default = _declared_default_interest(doc)
        if default is not None:
            state.default_interest = default
            state.default_interest_on = d.voucher_date
        for ref in doc.get("refs") or []:
            if isinstance(ref, dict):
                _apply_ref(state, ref, d.voucher_no, d.voucher_date)
    return states


def _rate_for(
    bill: Bill, state: PartyState, rate_override: Decimal | None, grace_override: int | None
) -> tuple[Decimal | None, int]:
    """Rate & grace resolution (ADR-023 §3): the bill's own declaration →
    the party's declared default (latest on or before the bill's opening) →
    the request's explicit parameters → none."""
    if bill.interest_annual is not None and bill.interest_annual > 0:
        return bill.interest_annual, bill.grace_days
    if (
        state.default_interest is not None
        and state.default_interest_on is not None
        and state.default_interest_on <= bill.opened_on
    ):
        return state.default_interest
    return rate_override, (grace_override or 0)


def _interest_on(bill: Bill, rate: Decimal, days: int) -> Decimal:
    """outstanding × rate% p.a. × days ÷ 365 — exact division at 50-digit
    precision, quantized to cents once, at the end (Law 3)."""
    per_diem = bill.outstanding * rate / _HUNDRED / _YEAR_DAYS
    return _money(per_diem * days)


# ------------------------------------------------------- the interest charge
def _overdue_interest_rows(
    resolver: Resolver,
    party: str,
    as_of: date,
    rate_override: Decimal | None,
    grace_override: int | None,
) -> list[dict]:
    """The computation both the report and the charge mode run: one row per
    overdue open bill of ``party`` (all parties when ``party`` is empty),
    with the resolved rate, days overdue and the exact interest."""
    states = fold(resolver.prior_documents(META_KEY), as_of=as_of)
    rows: list[dict] = []
    for name in sorted(states):
        if party and name != party:
            continue
        state = states[name]
        for bill in sorted(state.open_bills(), key=lambda b: (b.opened_on, b.ref)):
            rate, grace = _rate_for(bill, state, rate_override, grace_override)
            days = (as_of - (bill.due_on + timedelta(days=grace))).days
            if days <= 0:
                continue
            interest = _interest_on(bill, rate, days) if rate is not None else Decimal("0.00")
            rows.append(
                {
                    "party": name,
                    "ref": bill.ref,
                    "due_on": bill.due_on,
                    "outstanding": bill.outstanding,
                    "days_overdue": days,
                    "rate": rate,  # None = no rate resolvable
                    "interest": interest,
                }
            )
    return rows


def build_interest_charge(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    """The interest_charge document mode — the pack's only line-builder.
    Derives Dr party / Cr Interest Income from the overdue fold, and
    auto-opens one bill for the total (the master theorem demands the
    charge appear in the outstanding position)."""
    doc = draft.meta.get(META_KEY)
    if doc is None or not isinstance(doc, dict) or doc.get("interest_charge") is None:
        return draft
    if not isinstance(doc["interest_charge"], dict):
        raise ValidationError("bills: interest_charge must be an object")
    for other in ("gst", "invoice", "receipt", "stock", "payroll"):
        if draft.meta.get(other) is not None:
            raise ValidationError(
                f"bills: interest_charge and meta.{other} are mutually exclusive "
                "(the charge derives its own lines — post it alone)"
            )
    if doc.get("refs"):
        raise ValidationError(
            "bills: interest_charge derives its own allocations — explicit refs would be ambiguous"
        )

    party = str(doc.get("party") or "").strip()
    if not party:
        raise ValidationError("bills: interest_charge.party is required")
    party_ledger = resolver.ledger(name=party)
    if party_ledger is None:
        raise NotFoundError(
            f"bills: party ledger {party!r} does not exist in this book — bills "
            "never invent masters; create the ledger first"
        )

    charge = doc["interest_charge"]
    as_of = draft.voucher_date
    if charge.get("as_of") is not None:
        try:
            as_of = date.fromisoformat(str(charge["as_of"]))
        except ValueError:
            raise ValidationError("bills: interest_charge.as_of must be YYYY-MM-DD") from None
        if as_of != draft.voucher_date:
            raise ValidationError(
                "bills: interest_charge.as_of must equal the voucher date — the "
                "preview and the posting must agree on the fold they ran"
            )

    rate_override = None
    if charge.get("rate") is not None:
        rate_override = _amount(charge["rate"], "interest_charge.rate")
        if rate_override <= 0:
            raise ValidationError("bills: interest_charge.rate must be > 0")
    grace_override = None
    if charge.get("grace_days") is not None:
        try:
            grace_override = int(charge["grace_days"])
        except (TypeError, ValueError):
            raise ValidationError("bills: interest_charge.grace_days must be an integer") from None
        if grace_override < 0:
            raise ValidationError("bills: interest_charge.grace_days must be >= 0")

    rows = _overdue_interest_rows(resolver, party, as_of, rate_override, grace_override)
    chargeable = [r for r in rows if r["rate"] is not None and r["interest"] > 0]
    unpriced = [r["ref"] for r in rows if r["rate"] is None]
    if unpriced:
        raise ValidationError(
            "bills: no interest rate resolves for overdue bill(s) "
            f"{', '.join(unpriced)} — declare interest on the bill, a "
            "default_interest for the party, or an explicit rate"
        )
    if not chargeable:
        raise ValidationError(
            f"bills: {party} has no overdue open bills with interest to charge as "
            f"of {as_of.isoformat()}"
        )

    income = resolver.ensure_ledger(INTEREST_INCOME_LEDGER, INTEREST_INCOME_GROUP)
    total = sum((r["interest"] for r in chargeable), Decimal("0.00"))
    lines: list[DraftLine] = [
        DraftLine(
            dc="D",
            amount=r["interest"],
            ledger_id=party_ledger.id,
            ledger_name=party_ledger.name,
            narration=(
                f"interest {r['rate'].normalize():f}% p.a. on {r['ref']}, "
                f"{r['days_overdue']} days over {format_amount(r['outstanding'])}"
            ),
        )
        for r in chargeable
    ]
    lines.append(
        DraftLine(
            dc="C",
            amount=total,
            ledger_id=income.id,
            ledger_name=income.name,
            narration=f"interest income on {party}'s overdue bills",
        )
    )

    # The charge must appear in the outstanding position (the master
    # theorem): one auto-opened bill for the total, due immediately.
    ref = str(charge.get("ref") or "INTEREST").strip() or "INTEREST"
    doc["refs"] = [{"kind": "new", "ref": ref, "dr": str(total), "due_days": 0}]
    doc["computed"] = {
        "as_of": as_of.isoformat(),
        "charge_ref": ref,
        "lines": [
            {
                "ref": r["ref"],
                "due_on": r["due_on"].isoformat(),
                "outstanding": str(r["outstanding"]),
                "days_overdue": str(r["days_overdue"]),
                "rate": f"{r['rate'].normalize():f}",
                "interest": str(r["interest"]),
            }
            for r in chargeable
        ],
        "total": str(total),
    }
    return draft.with_lines(lines)


# ------------------------------------------------------------ the validator
def validate_allocations(draft: VoucherDraft, resolver: Resolver) -> None:
    """Runs after every transformer (the engine validates post-transform),
    so the party's lines are final — document-mode or plain. Enforces the
    conservation law and the settlement guards against the live fold."""
    doc = draft.meta.get(META_KEY)
    if doc is None:
        return
    if not isinstance(doc, dict):
        raise ValidationError("bills: meta.bills must be an object")

    party = str(doc.get("party") or "").strip()
    if not party:
        raise ValidationError("bills: party is required")
    if draft.meta.get("payroll") is not None:
        raise ValidationError(
            "bills: meta.bills and meta.payroll are mutually exclusive — payroll "
            "payables are a different model (out of scope, ADR-023)"
        )

    raw_refs = doc.get("refs") or []
    if not isinstance(raw_refs, list) or any(not isinstance(r, dict) for r in raw_refs):
        raise ValidationError("bills: refs must be a list of objects")
    refs = raw_refs

    if not refs:
        if _declared_default_interest(doc) is None and doc.get("interest_charge") is None:
            raise ValidationError(
                "bills: a bills document needs refs, a default_interest declaration, "
                "or an interest_charge — this one does nothing"
            )
        if doc.get("interest_charge") is not None:
            # the charge mode's refs were derived by the transformer; fall
            # through to the full checks with them
            refs = doc.get("refs") or []
            if not refs:  # pragma: no cover — the transformer always sets them
                return
        else:
            return  # a pure declaration: nothing to conserve

    party_ledger = resolver.ledger(name=party)
    if party_ledger is None:
        raise NotFoundError(
            f"bills: party ledger {party!r} does not exist in this book — bills "
            "never invent masters; create the ledger first"
        )

    # the party's net movement on this voucher (document-mode lines carry
    # ledger_id; plain lines may still be name-only — match either)
    party_dr = Decimal("0.00")
    party_cr = Decimal("0.00")
    moved = False
    for line in draft.lines:
        if line.ledger_id == party_ledger.id or line.ledger_name == party:
            if line.dc == "D":
                party_dr += line.amount
            else:
                party_cr += line.amount
            moved = True
    if not moved:
        raise ValidationError(
            f"bills: this voucher does not move party ledger {party!r} — "
            "allocations ride vouchers that do"
        )

    refs_dr = Decimal("0.00")
    refs_cr = Decimal("0.00")
    for ref in refs:
        where = f"ref {ref.get('ref', ref.get('kind'))}"
        dr, cr = _ref_amount(ref, where)
        refs_dr += dr
        refs_cr += cr

    net_party = party_dr - party_cr
    net_refs = refs_dr - refs_cr
    if net_refs != net_party:
        raise ValidationError(
            f"bills: allocations total {format_amount(net_refs)} but the voucher "
            f"moves {party!r} by {format_amount(net_party)} — refs and party lines "
            "must agree exactly (on_account absorbs the remainder)"
        )

    # settlement guards: replay the book's fold, then apply this voucher's
    # refs with the policy checks live against what is already posted
    state = fold(resolver.prior_documents(META_KEY)).get(party, PartyState(party=party))
    for ref in refs:
        _apply_ref(state, ref, "(this voucher)", draft.voucher_date)


# --------------------------------------------------------- event handler
def record_posted(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    meta = event.data.get("meta") or {}
    doc = meta.get(META_KEY)
    if not isinstance(doc, dict):
        return
    POSTED.append(
        {
            "voucher_no": event.data.get("voucher_no"),
            "book_id": event.data.get("book_id"),
            "party": doc.get("party"),
            "refs": len(doc.get("refs") or []),
            "interest_charge": doc.get("interest_charge") is not None,
        }
    )
    logger.info("bills document posted: %s", event.data.get("voucher_no"))


# ------------------------------------------------------------- report shape
def _bucket_for(bill: Bill, as_of: date, grace: int = 0) -> str:
    days = (as_of - (bill.due_on + timedelta(days=grace))).days
    if days <= 0:
        return "not due"
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def _fmt_rate(rate: Decimal | None) -> str:
    return f"{rate.normalize():f}%" if rate is not None else "-"


def _csv_rate(rate: Decimal | None) -> str:
    return f"{rate.normalize():f}" if rate is not None else ""


@dataclass(frozen=True)
class _OutstandingRow:
    party: str
    ref: str
    opened_on: date
    due_on: date
    outstanding: Decimal
    bucket: str
    interest_annual: Decimal | None


def _outstanding_rows(
    states: dict[str, PartyState], side: str, as_of: date, party: str | None
) -> tuple[list[_OutstandingRow], dict[str, Decimal]]:
    """Open bills on one side (dr: receivable, cr: payable), oldest first,
    bucketed; plus each party's on-account pool (signed to the same side)."""
    rows: list[_OutstandingRow] = []
    on_account: dict[str, Decimal] = {}
    for name in sorted(states):
        if party and name != party:
            continue
        state = states[name]
        for bill in sorted(state.open_bills(), key=lambda b: (b.opened_on, b.ref)):
            if (bill.outstanding > 0) != (side == "dr"):
                continue
            rows.append(
                _OutstandingRow(
                    party=name,
                    ref=bill.ref,
                    opened_on=bill.opened_on,
                    due_on=bill.due_on,
                    outstanding=bill.outstanding,
                    bucket=_bucket_for(bill, as_of),
                    interest_annual=bill.interest_annual,
                )
            )
        if state.on_account != 0 and (state.on_account > 0) == (side == "dr"):
            on_account[name] = state.on_account
    return rows, on_account


def _ageing_sections(
    rows: list[_OutstandingRow], on_account: dict[str, Decimal]
) -> tuple[list[tuple[str, Decimal]], list[tuple[str, Decimal]]]:
    """(per-party subtotals incl. on-account, bucket totals) — the ageing
    skeleton the view, CSV and JSON all share."""
    by_party: dict[str, Decimal] = {}
    for row in rows:
        by_party[row.party] = by_party.get(row.party, Decimal("0.00")) + row.outstanding
    for name, amount in on_account.items():
        by_party[name] = by_party.get(name, Decimal("0.00")) + amount
    buckets: dict[str, Decimal] = {label: Decimal("0.00") for label in AGEING_BUCKETS}
    for row in rows:
        buckets[row.bucket] += row.outstanding
    return (
        [(p, by_party[p]) for p in sorted(by_party)],
        [(label, buckets[label]) for label in AGEING_BUCKETS],
    )


def _bills_view(
    side: str, rows: list[_OutstandingRow], on_account: dict[str, Decimal], as_of: date
) -> DocumentView:
    label = "Bills receivable" if side == "dr" else "Bills payable"
    section_rows = [
        (
            r.party,
            r.ref,
            r.opened_on.isoformat(),
            r.due_on.isoformat(),
            format_amount(r.outstanding),
            r.bucket,
            _fmt_rate(r.interest_annual),
        )
        for r in rows
    ]
    for name in sorted(on_account):
        section_rows.append(
            (name, "on account", "-", "-", format_amount(on_account[name]), "-", "-")
        )

    party_totals, bucket_totals = _ageing_sections(rows, on_account)
    total = sum((amount for _p, amount in party_totals), Decimal("0.00"))
    footnotes = [
        "Open bills replayed from posted meta.bills documents with reversals "
        "netted (Law 2); the outstanding position is derived, never stored.",
        "Ageing is by due date: not due / 0-30 / 31-60 / 61-90 / 90+ days "
        "overdue as of the as-of date.",
        "On-account amounts are the party's unallocated pool — surfaced, never "
        "auto-applied to bills.",
        "Every party's bills + on-account equal its ledger balance (the "
        "conservation law of ADR-023, inductive over posted vouchers).",
    ]
    if not rows and not on_account:
        footnotes.append(f"No {label.lower()} outstanding as of {as_of.isoformat()}.")

    return DocumentView(
        title=label,
        subtitle=f"outstanding with ageing — as of {as_of.isoformat()}",
        fields=(
            ("As of", as_of.isoformat()),
            ("Open bills", str(len(rows))),
            ("Parties", str(len({r.party for r in rows}))),
        ),
        sections=(
            DocumentSection(
                title="Open bills",
                columns=(
                    DocumentColumn("Party", width=1.5),
                    DocumentColumn("Ref", width=1.1),
                    DocumentColumn("Opened", width=0.85),
                    DocumentColumn("Due", width=0.85),
                    DocumentColumn("Outstanding", width=1.0, align="right"),
                    DocumentColumn("Ageing", width=0.7),
                    DocumentColumn("Rate", width=0.6, align="right"),
                ),
                rows=tuple(section_rows),
            ),
        ),
        totals=(
            *((f"Ageing — {bucket}", format_amount(amount)) for bucket, amount in bucket_totals),
            (f"Total {label.lower()}", format_amount(total)),
        ),
        footnotes=tuple(footnotes),
        filename=f"{label} {as_of.isoformat()}",
    )


def _parse_as_of(params: dict[str, str]) -> date:
    raw = (params.get("as_of") or "").strip()
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError("bills: as_of must be YYYY-MM-DD") from None


def _parse_party(params: dict[str, str], required: bool) -> str:
    party = (params.get("party") or "").strip()
    if required and not party:
        raise ValidationError("bills: party is required")
    return party


def _outstanding_data(
    side: str,
    rows: list[_OutstandingRow],
    on_account: dict[str, Decimal],
    as_of: date,
    party: str | None,
) -> dict:
    party_totals, bucket_totals = _ageing_sections(rows, on_account)
    return {
        "side": side,
        "as_of": as_of.isoformat(),
        "party": party,
        "bills": [
            {
                "party": r.party,
                "ref": r.ref,
                "opened_on": r.opened_on.isoformat(),
                "due_on": r.due_on.isoformat(),
                "outstanding": str(r.outstanding),
                "bucket": r.bucket,
                "interest_annual": _csv_rate(r.interest_annual) or None,
            }
            for r in rows
        ],
        "on_account": {name: str(amount) for name, amount in sorted(on_account.items())},
        "party_totals": {p: str(a) for p, a in party_totals},
        "ageing": {label: str(a) for label, a in bucket_totals},
        "total": str(sum((a for _p, a in party_totals), Decimal("0.00"))),
    }


def _outstanding_report(resolver: Resolver, params: dict[str, str], side: str) -> PackReport:
    as_of = _parse_as_of(params)
    party = _parse_party(params, required=False) or None
    label = "Bills receivable" if side == "dr" else "Bills payable"
    states = fold(resolver.prior_documents(META_KEY), as_of=as_of)
    rows, on_account = _outstanding_rows(states, side, as_of, party)
    data = _outstanding_data(side, rows, on_account, as_of, party)
    title = f"{label} {as_of.isoformat()}"
    if (params.get("format") or "").lower() == "csv":
        lines = ["party,ref,opened_on,due_on,outstanding,ageing,interest_annual"]
        lines += [
            f"{r.party},{r.ref},{r.opened_on.isoformat()},{r.due_on.isoformat()},"
            f"{r.outstanding},{r.bucket},{_csv_rate(r.interest_annual)}"
            for r in rows
        ]
        for name, amount in sorted(on_account.items()):
            lines.append(f"{name},on account,-,-,{amount},-,-")
        lines.append(f"TOTAL,-,-,-,{data['total']},-,-")
        return PackReport(title=title, filename=title, csv="\n".join(lines) + "\n", data=data)
    return PackReport(
        title=title,
        filename=title,
        view=_bills_view(side, rows, on_account, as_of),
        data=data,
    )


def receivable_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Open receivable bills with ageing — PDF (default), CSV or JSON."""
    return _outstanding_report(resolver, params, "dr")


def payable_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Open payable bills with ageing — PDF (default), CSV or JSON."""
    return _outstanding_report(resolver, params, "cr")


def open_bills_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """One party's open bills (both sides) + on-account — the allocation
    dialog's feed and the party-wise print."""
    party = _parse_party(params, required=True)
    as_of = _parse_as_of(params)
    ledger = resolver.ledger(name=party)
    if ledger is None:
        raise NotFoundError(f"bills: party ledger {party!r} does not exist in this book")
    states = fold(resolver.prior_documents(META_KEY), as_of=as_of)
    state = states.get(party, PartyState(party=party))
    bills = sorted(state.open_bills(), key=lambda b: (b.opened_on, b.ref))
    data = {
        "party": party,
        "as_of": as_of.isoformat(),
        "bills": [
            {
                "ref": b.ref,
                "side": b.side,
                "opened_on": b.opened_on.isoformat(),
                "due_on": b.due_on.isoformat(),
                "outstanding": str(b.outstanding),
                "bucket": _bucket_for(b, as_of),
                "interest_annual": _csv_rate(b.interest_annual) or None,
            }
            for b in bills
        ],
        "on_account": str(state.on_account),
    }
    title = f"Open bills {party}"
    if (params.get("format") or "").lower() == "csv":
        lines = ["ref,side,opened_on,due_on,outstanding,ageing"]
        lines += [
            f"{b.ref},{b.side},{b.opened_on.isoformat()},{b.due_on.isoformat()},"
            f"{b.outstanding},{_bucket_for(b, as_of)}"
            for b in bills
        ]
        lines.append(f"on account,-,-,-,{state.on_account},-")
        return PackReport(title=title, filename=title, csv="\n".join(lines) + "\n", data=data)
    return PackReport(
        title=title,
        filename=title,
        view=DocumentView(
            title=f"Open bills — {party}",
            subtitle=f"as of {as_of.isoformat()}",
            fields=(
                ("Party", party),
                ("Open bills", str(len(bills))),
                ("On account", format_amount(state.on_account)),
            ),
            sections=(
                DocumentSection(
                    title="Open bills",
                    columns=(
                        DocumentColumn("Ref", width=1.4),
                        DocumentColumn("Side", width=0.5),
                        DocumentColumn("Opened", width=0.85),
                        DocumentColumn("Due", width=0.85),
                        DocumentColumn("Outstanding", width=1.0, align="right"),
                        DocumentColumn("Ageing", width=0.7),
                    ),
                    rows=tuple(
                        (
                            b.ref,
                            b.side,
                            b.opened_on.isoformat(),
                            b.due_on.isoformat(),
                            format_amount(b.outstanding),
                            _bucket_for(b, as_of),
                        )
                        for b in bills
                    ),
                ),
            ),
            totals=(
                ("On account", format_amount(state.on_account)),
                (
                    "Net position",
                    format_amount(
                        sum((b.outstanding for b in bills), Decimal("0.00")) + state.on_account
                    ),
                ),
            ),
            footnotes=(
                "Both sides shown: dr bills the party owes you, cr bills are "
                "advances/credits. The net position equals the party's ledger "
                "balance (ADR-023's conservation law).",
            ),
            filename=f"Open bills {party}",
        ),
        data=data,
    )


# ------------------------------------------------------- the ledger statement
def _statement_rows(resolver: Resolver, party: str, ledger_id: int) -> list[dict]:
    """The party's running statement: every posted entry (reversals as their
    own rows, marked), each voucher's bill allocations joined in, running
    balance from the ledger's first entry."""
    entries: list[LedgerEntryLine] = resolver.ledger_entries(ledger_id)
    if not entries:
        raise NotFoundError(f"bills: {party!r} has no posted entries — nothing to state")
    # this party's bills documents by voucher id, for the bill-link column
    doc_by_voucher: dict[int, dict] = {}
    for d in resolver.prior_documents(META_KEY):
        doc = (d.meta or {}).get(META_KEY)
        if isinstance(doc, dict) and str(doc.get("party") or "") == party:
            doc_by_voucher[d.voucher_id] = doc

    balance = Decimal("0.00")
    rows: list[dict] = []
    for e in entries:
        signed = e.amount if e.dc == "D" else -e.amount
        balance = _money(balance + signed)  # cents, always — NUMERIC sums keep 8
        doc = doc_by_voucher.get(e.voucher_id) or {}
        links: list[str] = []
        for ref in doc.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            kind = ref.get("kind")
            if kind == "on_account":
                links.append("on a/c")
                continue
            name = str(ref.get("ref") or "").strip()
            if not name:
                continue
            dr, cr = ref.get("dr"), ref.get("cr")
            links.append(f"{name} {'Dr' if cr is None else 'Cr'} {dr if cr is None else cr}")
        rows.append(
            {
                "voucher_no": e.voucher_no,
                "date": e.voucher_date,
                "narration": e.narration or "",
                "dc": e.dc,
                "amount": e.amount,
                "balance": balance,
                "bills": "; ".join(links),
                "reversal_of": e.reversal_of,
            }
        )
    return rows


def statement_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The party's running statement — opening balance to date, bill-linked.
    PDF (default), CSV or JSON."""
    party = _parse_party(params, required=True)
    ledger = resolver.ledger(name=party)
    if ledger is None:
        raise NotFoundError(f"bills: party ledger {party!r} does not exist in this book")

    date_from = None
    date_to = None
    if (params.get("from") or "").strip():
        try:
            date_from = date.fromisoformat(params["from"].strip())
        except ValueError:
            raise ValidationError("bills: from must be YYYY-MM-DD") from None
    if (params.get("to") or "").strip():
        try:
            date_to = date.fromisoformat(params["to"].strip())
        except ValueError:
            raise ValidationError("bills: to must be YYYY-MM-DD") from None

    all_rows = _statement_rows(resolver, party, ledger.id)
    if date_from is None:
        opening = Decimal("0.00")
    else:
        prior = [r for r in all_rows if r["date"] < date_from]
        opening = prior[-1]["balance"] if prior else Decimal("0.00")
    window = [
        r
        for r in all_rows
        if (date_from is None or r["date"] >= date_from)
        and (date_to is None or r["date"] <= date_to)
    ]
    closing = window[-1]["balance"] if window else opening

    def _fmt_dc(r: dict) -> str:
        amount = format_amount(r["amount"])
        return f"{amount} Dr" if r["dc"] == "D" else f"{amount} Cr"

    data = {
        "party": party,
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "opening": str(opening),
        "closing": str(closing),
        "rows": [
            {
                "date": r["date"].isoformat(),
                "voucher_no": r["voucher_no"],
                "narration": r["narration"],
                "dc": r["dc"],
                "amount": str(r["amount"]),
                "balance": str(r["balance"]),
                "bills": r["bills"],
                "reversal_of": r["reversal_of"],
            }
            for r in window
        ],
    }
    title = f"Ledger statement {party}"
    if (params.get("format") or "").lower() == "csv":
        lines = ["date,voucher,narration,bills,dc,amount,balance"]
        lines += [
            f'{r["date"].isoformat()},{r["voucher_no"]},"{r["narration"]}",'
            f'"{r["bills"]}",{r["dc"]},{r["amount"]},{r["balance"]}'
            for r in window
        ]
        lines.append(f"OPENING,-,-,-,-,-,{opening}")
        lines.append(f"CLOSING,-,-,-,-,-,{closing}")
        return PackReport(title=title, filename=title, csv="\n".join(lines) + "\n", data=data)
    return PackReport(
        title=title,
        filename=title,
        view=DocumentView(
            title=f"Ledger statement — {party}",
            subtitle=(
                f"{date_from.isoformat() if date_from else 'the first entry'} to "
                f"{date_to.isoformat() if date_to else 'date'}"
            ),
            fields=(
                ("Party", party),
                ("Opening balance", format_amount(opening)),
                ("Entries", str(len(window))),
            ),
            sections=(
                DocumentSection(
                    title="Statement",
                    columns=(
                        DocumentColumn("Date", width=0.8),
                        DocumentColumn("Voucher", width=1.3),
                        DocumentColumn("Particulars", width=2.4),
                        DocumentColumn("Bill links", width=1.6),
                        DocumentColumn("Amount", width=1.0, align="right"),
                        DocumentColumn("Balance", width=1.0, align="right"),
                    ),
                    rows=tuple(
                        (
                            r["date"].isoformat(),
                            r["voucher_no"],
                            r["narration"] or ("reversal" if r["reversal_of"] else ""),
                            r["bills"],
                            _fmt_dc(r),
                            format_amount(r["balance"]),
                        )
                        for r in window
                    ),
                ),
            ),
            totals=(
                ("Opening balance", format_amount(opening)),
                ("Closing balance", format_amount(closing)),
            ),
            footnotes=(
                "Every posted entry on the party ledger, oldest first, with the "
                "voucher's bill allocations alongside — the accounting line and "
                "its bill-wise meaning on one row.",
                "The closing balance equals the party's trial-balance figure: "
                "bills plus on-account equal the ledger by construction (ADR-023).",
            ),
            filename=f"Ledger statement {party}",
        ),
        data=data,
    )


# ------------------------------------------------------- the interest report
def interest_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """Interest on overdue open bills — the preview of what the charge mode
    posts. PDF (default), CSV or JSON."""
    as_of = _parse_as_of(params)
    party = _parse_party(params, required=False) or None
    rate_override = None
    if (params.get("rate") or "").strip():
        rate_override = _amount(params["rate"], "rate")
        if rate_override <= 0:
            raise ValidationError("bills: rate must be > 0")
    grace_override = None
    if (params.get("grace_days") or "").strip():
        try:
            grace_override = int(params["grace_days"])
        except ValueError:
            raise ValidationError("bills: grace_days must be an integer") from None
        if grace_override < 0:
            raise ValidationError("bills: grace_days must be >= 0")

    rows = _overdue_interest_rows(resolver, party or "", as_of, rate_override, grace_override)
    chargeable = [r for r in rows if r["rate"] is not None]
    unpriced = [r for r in rows if r["rate"] is None]
    total = sum((r["interest"] for r in chargeable), Decimal("0.00"))
    data = {
        "as_of": as_of.isoformat(),
        "party": party,
        "rows": [
            {
                "party": r["party"],
                "ref": r["ref"],
                "due_on": r["due_on"].isoformat(),
                "outstanding": str(r["outstanding"]),
                "days_overdue": str(r["days_overdue"]),
                "rate": _csv_rate(r["rate"]) or None,
                "interest": str(r["interest"]),
            }
            for r in rows
        ],
        "total": str(total),
        "unpriced_refs": [r["ref"] for r in unpriced],
    }
    title = f"Interest on overdue bills {as_of.isoformat()}"
    if (params.get("format") or "").lower() == "csv":
        lines = ["party,ref,due_on,outstanding,days_overdue,rate,interest"]
        lines += [
            f"{r['party']},{r['ref']},{r['due_on'].isoformat()},{r['outstanding']},"
            f"{r['days_overdue']},{_csv_rate(r['rate'])},{r['interest']}"
            for r in rows
        ]
        lines.append(f"TOTAL,-,-,-,-,-,{total}")
        return PackReport(
            title=title,
            filename=f"Interest {as_of.isoformat()}",
            csv="\n".join(lines) + "\n",
            data=data,
        )
    view = DocumentView(
        title="Interest on overdue bills",
        subtitle=f"computed as of {as_of.isoformat()}",
        fields=(
            ("As of", as_of.isoformat()),
            ("Overdue bills", str(len(rows))),
            ("Total interest", format_amount(total)),
        ),
        sections=(
            DocumentSection(
                title="Overdue open bills",
                columns=(
                    DocumentColumn("Party", width=1.4),
                    DocumentColumn("Ref", width=1.0),
                    DocumentColumn("Due", width=0.85),
                    DocumentColumn("Outstanding", width=1.0, align="right"),
                    DocumentColumn("Days", width=0.55, align="right"),
                    DocumentColumn("Rate", width=0.6, align="right"),
                    DocumentColumn("Interest", width=0.9, align="right"),
                ),
                rows=tuple(
                    (
                        r["party"],
                        r["ref"],
                        r["due_on"].isoformat(),
                        format_amount(r["outstanding"]),
                        str(r["days_overdue"]),
                        _fmt_rate(r["rate"]),
                        format_amount(r["interest"]),
                    )
                    for r in rows
                ),
            ),
        ),
        totals=(("Total interest", format_amount(total)),),
        footnotes=(
            "interest = outstanding × rate% p.a. × days overdue ÷ 365, exact "
            "decimals, rounded once at the end (Law 3); days count from the due "
            "date plus the resolved grace days.",
            "Rate resolution: the bill's own declaration → the party's "
            "default_interest → this report's rate parameter → none.",
            *(
                (
                    f"No rate resolves for: {', '.join(r['ref'] for r in unpriced)} — "
                    "shown without interest (a blank is honest).",
                )
                if unpriced
                else ()
            ),
            "Posting is a voucher, never a side effect: post meta.bills with "
            "interest_charge (or the lines by hand) — the charge opens a bill "
            "for its total.",
        ),
        filename=f"Interest {as_of.isoformat()}",
    )
    return PackReport(
        title=title,
        filename=f"Interest {as_of.isoformat()}",
        view=view,
        data=data,
    )
