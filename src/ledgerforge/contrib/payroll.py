"""Payroll plugin pack: salaries as documents on the voucher store (ADR-012).

The roadmap's promise (analysis §9) is that payroll is "a plugin on the same
hook taxonomy + document store; the v0.7 read/lock primitives are its
building blocks" — this pack is that promise kept, the third business pack
after invoicing and inventory, landing with **zero schema change**.

Two document modes, both on ``meta.payroll``:

- ``mode: "run"`` — the payroll run for a period (``"YYYY-MM"``): employees
  × components. A component is one pay-head line with a ``kind``, the
  TallyPrime pay-head taxonomy in miniature:

  - ``earning`` — Dr the component's expense ledger (default under
    *Indirect Expenses*). "Basic Salary", "HRA", "Overtime"…
  - ``withheld`` — Cr the component's liability ledger (default under
    *Duties & Taxes*): PF/ESI/professional tax withheld from the employee.
    It reduces net pay; the expense was booked at gross.
  - ``employer`` — the employer's contribution: Dr an expense ledger AND Cr
    a liability (``liability`` is required — commonly the same "PF Payable"
    the withheld side feeds). It never touches net pay.

  Net pay = Σ earnings − Σ withheld, credited to the employee's payable
  ledger (default: the employee's name under *Provisions*). One voucher
  carries the whole run — the TallyPrime payroll journal shape.

- ``mode: "payout"`` — salary settlement: Dr the employee's payable, Cr the
  bank (under *Bank Accounts*). The invoicing pack's receipt, mirrored.

The derived-state contract (the part tests pin down):

- **Year-to-date is a fiscal-year fold** over prior run documents — same FY
  (the book's ``fy_start_month``, plugin API 0.4), periods ≤ the posting
  run's, reversals netted out. The transformer annotates per-employee and
  per-component YTD into ``meta.payroll.computed`` — the payslip data — and
  ``ytd(resolver, employee)`` replays the same fold for any later consumer.
- **Payable is an all-time balance**, never FY-windowed: accrued net minus
  payouts across all years (a balance never expires; an accumulator
  windows). ``payable(resolver, employee)`` replays it; the payout guard is
  policy, not law — overpaying a payable ledger is book-legal (it goes
  negative = a recoverable advance) but defaults to a 422 with
  ``policy:allow_overpay`` as the explicit escape.
- **One run per employee per period** — checked against prior documents
  (reversed runs don't count), serialized by ``resolver.lock`` on
  ``payroll:{employee}`` in one sorted pass, so concurrent runs of the same
  employee cannot both pass the guard.
- The transformer and every later re-derivation run the **same fold**
  (``_fold``) over the same document sequence — deterministic replays,
  never stored state.

Reversal semantics (ADR-010/011): a reversal carries empty meta, so
``prior_documents`` returns it alongside its original; the fold skips the
undone original — an original + its reversal net to zero on YTD and payable,
matching what the mirrored entries did to the ledgers.

All money is exact ``Decimal`` (Law 3); every component amount is quantized
to cents at parse, and the run posts at the client's exact figures — the
pack adds no rounding of its own. Attendance-derived pay computation, leave
encashment and per-employee statutory caps are deliberately out of scope:
amounts arrive as exact figures, and computing them is the caller's (or a
future pack's) job. The ``RUNS`` list (after-commit seam) records every
posted payroll document — the spot a payslip PDF generator or a PF filing
bridge subscribes to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..errors import ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentContext,
    DocumentSection,
    DocumentView,
    DraftLine,
    PluginManifest,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.payroll")

PLUGIN = PluginManifest(name="payroll", version="0.3.0", api_compat="0.5")

META_KEY = "payroll"

# Seed groups (ledgerforge/seeding.py) — masters resolve/create through the
# book-scoped Resolver only, so everything stays per-book (ADR-009).
EXPENSE_GROUP = "Indirect Expenses"  # earnings + employer contributions
STATUTORY_GROUP = "Duties & Taxes"  # withheld + employer liabilities
DEFAULT_PAYABLE_GROUP = "Provisions"  # employee net-pay ledgers
BANK_GROUP = "Bank Accounts"  # payout settlement

#: Every payroll document posted while this pack is loaded (observability
#: for tests/demos; the real-world consumer is a payslip/PF-filing bridge).
RUNS: list[dict] = []

CENT = Decimal("0.01")
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
MODES_VALID = ("run", "payout")
KINDS_VALID = ("earning", "withheld", "employer")


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_payroll_document)
    registry.register_validator(no_future_document_dates)
    registry.on("voucher.posted", record_run)
    registry.register_document_view(META_KEY, payslip_view)


# --------------------------------------------------------------- helpers
def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"payroll: {field_name} must be a number, got {value!r}") from None
    if not amount.is_finite():
        raise ValidationError(f"payroll: {field_name} must be a finite number")
    return _money(amount)


def _override(item: dict, key: str) -> str | None:
    """A stripped explicit override, or None (the default path)."""
    value = item.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_period(value: object) -> tuple[int, int, str]:
    """A payroll period is ``YYYY-MM`` (zero-padded) — a work month."""
    raw = str(value or "").strip()
    match = _PERIOD_RE.fullmatch(raw)
    if match is None:
        raise ValidationError(f"payroll: period must be YYYY-MM (e.g. 2026-10), got {value!r}")
    year, month = int(match[1]), int(match[2])
    if not 1 <= month <= 12:
        raise ValidationError(f"payroll: period month {month:02d} is not 01–12")
    return year, month, f"{year:04d}-{month:02d}"


def _fy_of(year: int, month: int, fy_start_month: int) -> int:
    """The fiscal-year label of a period: months >= fy_start_month open a
    year (April starts FY 2026-27 → label 2026); earlier months belong to
    the previous label. Pure calendar arithmetic on the book's setting."""
    return year if month >= fy_start_month else year - 1


def _fy_of_date(day: date, fy_start_month: int) -> int:
    return _fy_of(day.year, day.month, fy_start_month)


@dataclass
class _EmployeeState:
    """Derived payroll state of one employee. The FY-windowed view fills
    gross/withheld/employer/net/runs/components; the all-time view (fy=None)
    also fills ``paid`` — the payable side. Same fold, two windows: an
    accumulator windows by fiscal year, a balance never expires."""

    gross: Decimal = Decimal("0.00")
    withheld: Decimal = Decimal("0.00")
    employer: Decimal = Decimal("0.00")
    net: Decimal = Decimal("0.00")
    paid: Decimal = Decimal("0.00")
    runs: int = 0
    components: dict[tuple[str, str], Decimal] = field(default_factory=dict)


def _iter_employee_runs(doc: dict, employee: str):
    """The employee's entries inside one stored run document."""
    for entry in doc.get("employees") or []:
        if isinstance(entry, dict) and str(entry.get("employee") or "").strip() == employee:
            yield entry


def _fold(
    documents: list,
    employee: str,
    *,
    fy: int | None = None,
    upto: str | None = None,
    fy_start: int = 4,
) -> _EmployeeState:
    """Replay every posted payroll document of the book for ``employee``,
    oldest first. Documents undone by a reversal net to zero (the reversal
    marks its original, and both are skipped). With ``fy`` set, only runs of
    that fiscal year (computed against ``fy_start``, the book's setting)
    with period <= ``upto`` accumulate — the YTD window; payouts are always
    all-time (they settle a balance, not an accrual)."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    state = _EmployeeState()
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        mode = doc.get("mode")
        if mode == "run":
            for entry in _iter_employee_runs(doc, employee):
                try:
                    year, month, _ = _parse_period(doc.get("period"))
                except ValidationError:
                    continue  # a document this pack did not shape — not ours
                if fy is not None:
                    if _fy_of(year, month, fy_start) != fy:
                        continue
                    if upto is not None and f"{year:04d}-{month:02d}" > upto:
                        continue
                entry_gross = Decimal("0.00")
                entry_withheld = Decimal("0.00")
                entry_employer = Decimal("0.00")
                for comp in entry.get("components") or []:
                    if not isinstance(comp, dict):
                        continue
                    kind = str(comp.get("kind") or "")
                    try:
                        amount = _decimal(comp.get("amount"), "amount")
                    except ValidationError:
                        continue
                    if kind == "earning":
                        entry_gross += amount
                    elif kind == "withheld":
                        entry_withheld += amount
                    elif kind == "employer":
                        entry_employer += amount
                    key = (str(comp.get("name") or ""), kind)
                    state.components[key] = state.components.get(key, Decimal("0.00")) + amount
                state.gross += entry_gross
                state.withheld += entry_withheld
                state.employer += entry_employer
                state.net += entry_gross - entry_withheld
                state.runs += 1
        elif mode == "payout" and str(doc.get("employee") or "").strip() == employee:
            try:
                state.paid += _decimal(doc.get("amount"), "amount")
            except ValidationError:
                continue
    return state


def _has_relationship(documents: list, employee: str) -> bool:
    """Any non-reversed payroll document naming the employee — the gate for
    payouts (a payout settles a payroll relationship; a payment to a
    stranger is a plain payment voucher)."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        if doc.get("mode") == "payout" and str(doc.get("employee") or "").strip() == employee:
            return True
        if doc.get("mode") == "run" and any(True for _ in _iter_employee_runs(doc, employee)):
            return True
    return False


def _already_run(documents: list, employee: str, period: str) -> bool:
    """One run per employee per period — reversed runs don't count."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict) or doc.get("mode") != "run":
            continue
        if str(doc.get("period") or "") != period:
            continue
        if any(True for _ in _iter_employee_runs(doc, employee)):
            return True
    return False


# ------------------------------------------------------ public derived views
@dataclass(frozen=True)
class EmployeeYTD:
    """Fiscal-year-to-date accumulators of one employee — the statutory
    view (PF/ESI wage bases, Form-16 style totals)."""

    gross: Decimal
    withheld: Decimal
    employer: Decimal
    net: Decimal
    runs: int


def ytd(resolver: Resolver, employee: str, *, fy: int | None = None) -> EmployeeYTD:
    """The employee's YTD figures in the resolver's book — the same fold the
    transformer runs, so it always agrees with posted documents. ``fy``
    defaults to the fiscal year of today."""
    if fy is None:
        fy = _fy_of_date(date.today(), resolver.fy_start_month)
    state = _fold(
        resolver.prior_documents(META_KEY),
        employee,
        fy=fy,
        fy_start=resolver.fy_start_month,
    )
    return EmployeeYTD(
        gross=state.gross,
        withheld=state.withheld,
        employer=state.employer,
        net=state.net,
        runs=state.runs,
    )


@dataclass(frozen=True)
class EmployeePayable:
    """The employee's all-time payable position — accrued net minus payouts.
    Negative = the company holds a recoverable advance (policy-gated)."""

    accrued: Decimal
    paid: Decimal
    payable: Decimal


def payable(resolver: Resolver, employee: str) -> EmployeePayable:
    """The employee's payable balance in the resolver's book — the same
    all-time fold the payout guard runs."""
    global _fold_fy_start
    _fold_fy_start = resolver.fy_start_month
    state = _fold(resolver.prior_documents(META_KEY), employee)
    return EmployeePayable(accrued=state.net, paid=state.paid, payable=state.net - state.paid)


@dataclass(frozen=True)
class EmployeeWages:
    """One employee's wage detail for one period (ADR-014) — the statutory
    bridge's input. ``components`` maps ``(name, kind)`` to the amount
    summed across every run of the period; ``extras`` carries the entry's
    other fields exactly as posted (``uan``, ``esi_ip``… — the pack stores
    what the caller sends, so identifiers ride along)."""

    employee: str
    gross: Decimal
    components: dict[tuple[str, str], Decimal]
    extras: dict[str, Any]


def period_wages(resolver: Resolver, period: str) -> list[EmployeeWages]:
    """Every employee's wage detail for one ``YYYY-MM`` period — the same
    document sequence and the same reversal netting as the YTD fold, so a
    statutory statement (PF/ESI, ADR-014) always agrees with the payslips
    and the ledger. Off-cycle duplicates (``policy:allow_duplicate_run``)
    sum; employees sort by name for a deterministic report; payout
    documents never appear (they settle, they don't accrue wages)."""
    _year, _month, period = _parse_period(period)
    documents = resolver.prior_documents(META_KEY)
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    gross: dict[str, Decimal] = {}
    components: dict[str, dict[tuple[str, str], Decimal]] = {}
    extras: dict[str, dict[str, Any]] = {}

    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict) or doc.get("mode") != "run":
            continue
        if str(doc.get("period") or "").strip() != period:
            continue
        for entry in doc.get("employees") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("employee") or "").strip()
            if not name:
                continue
            gross.setdefault(name, Decimal("0.00"))
            components.setdefault(name, {})
            extras.setdefault(name, {})
            for comp in entry.get("components") or []:
                if not isinstance(comp, dict):
                    continue
                kind = str(comp.get("kind") or "")
                try:
                    amount = _decimal(comp.get("amount"), "amount")
                except ValidationError:
                    continue  # a document this pack did not shape — not ours
                if kind == "earning":
                    gross[name] += amount
                key = (str(comp.get("name") or ""), kind)
                components[name][key] = components[name].get(key, Decimal("0.00")) + amount
            for field_name, value in entry.items():
                if field_name not in ("employee", "components"):
                    extras[name][field_name] = value

    return [
        EmployeeWages(
            employee=name,
            gross=gross[name],
            components=components[name],
            extras=extras[name],
        )
        for name in sorted(gross)
    ]


# ------------------------------------------------------------- payslip print
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
    """``2026-10`` → ``October 2026`` (already validated at post time)."""
    raw = period.strip()
    if len(raw) == 7 and raw[4] == "-":
        try:
            return f"{_MONTHS[int(raw[5:7]) - 1]} {raw[:4]}"
        except (ValueError, IndexError):
            pass
    return raw


def _view_amount(value: object) -> str:
    """A stored money string (or missing) as display money."""
    try:
        return format_amount(Decimal(str(value)))
    except ArithmeticError:
        return str(value)


def payslip_view(ctx: DocumentContext) -> DocumentView | None:
    """The print layout for ``meta.payroll`` documents (plugin API 0.5,
    ADR-013) — the payslip PDF generator the pack was built to feed.

    Runs only: a run is one voucher for many employees, so the caller
    picks the subject with ``?employee=<name>`` (the sole employee of a
    single-employee run is the default). Every figure is the pack's own:
    per-component amounts and YTD from the voucher's immutable
    ``computed`` block, the payable position replayed through the same
    ``payable()`` fold the payout guard runs — a payslip that disagrees
    with the ledger is impossible by construction. Payouts decline
    (``None``) and print as the voucher slip.
    """
    doc = ctx.meta.get(META_KEY)
    if not isinstance(doc, dict) or doc.get("mode") != "run":
        return None

    raw_employees = [e for e in (doc.get("employees") or []) if isinstance(e, dict)]
    computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
    period = str(computed.get("period") or doc.get("period") or "")
    computed_by_name = {
        str(e.get("employee") or ""): e
        for e in (computed.get("employees") or [])
        if isinstance(e, dict)
    }
    names = [
        str(e.get("employee") or "").strip()
        for e in raw_employees
        if str(e.get("employee") or "").strip()
    ]

    requested = str(ctx.params.get("employee") or "").strip()
    if not requested and len(names) == 1:
        requested = names[0]
    if requested not in names:
        covered = ", ".join(names) if names else "no employees"
        if requested:
            raise ValidationError(
                f"payroll: {requested!r} has no run in this document — it covers: {covered}"
            )
        raise ValidationError(f"payroll: pass ?employee=<name> — this run covers: {covered}")

    entry = computed_by_name.get(requested) or next(
        e for e in raw_employees if str(e.get("employee") or "").strip() == requested
    )
    components = [c for c in (entry.get("components") or []) if isinstance(c, dict)]

    def rows_of(kind: str) -> list[tuple[str, ...]]:
        return [
            (
                str(c.get("name") or ""),
                _view_amount(c.get("amount")),
                _view_amount(c.get("ytd")) if c.get("ytd") is not None else "",
            )
            for c in components
            if str(c.get("kind") or "") == kind
        ]

    def total_of(key: str, kind: str) -> str:
        if entry.get(key) is not None:
            return _view_amount(entry.get(key))
        return _view_amount(
            sum(
                (
                    Decimal(str(c.get("amount") or "0"))
                    for c in components
                    if str(c.get("kind")) == kind
                ),
                Decimal("0.00"),
            )
        )

    ytd = entry.get("ytd") if isinstance(entry.get("ytd"), dict) else {}
    position = payable(ctx.resolver, requested)

    sections = [
        DocumentSection(
            title="Earnings",
            columns=(
                DocumentColumn("Component", width=2.6),
                DocumentColumn("Amount", width=1.0, align="right"),
                DocumentColumn("Year to date", width=1.0, align="right"),
            ),
            rows=tuple(rows_of("earning")),
        )
    ]
    if rows_of("withheld"):
        sections.append(
            DocumentSection(
                title="Deductions (withheld)",
                columns=(
                    DocumentColumn("Component", width=2.6),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("Year to date", width=1.0, align="right"),
                ),
                rows=tuple(rows_of("withheld")),
            )
        )
    if rows_of("employer"):
        sections.append(
            DocumentSection(
                title="Employer contributions",
                columns=(
                    DocumentColumn("Component", width=2.6),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("Year to date", width=1.0, align="right"),
                ),
                rows=tuple(rows_of("employer")),
            )
        )

    fields: list[tuple[str, str]] = [
        ("Employee", requested),
        ("Period", _period_label(period)),
        ("Pay date", ctx.voucher_date.isoformat()),
        ("Voucher", f"{ctx.voucher_type} {ctx.voucher_no}"),
    ]
    if ytd:
        ytd_line = (
            f"gross {_view_amount(ytd.get('gross'))}"
            f" · withheld {_view_amount(ytd.get('withheld'))}"
            f" · net {_view_amount(ytd.get('net'))}"
        )
        fields.append(("Fiscal-year YTD", ytd_line))
    fields.append(
        (
            "Salary payable",
            f"accrued {_view_amount(position.accrued)} - paid {_view_amount(position.paid)}"
            f" = due {_view_amount(position.payable)}",
        )
    )

    return DocumentView(
        title="Payslip",
        subtitle=f"Salary for {_period_label(period)} · {ctx.book_name}",
        reference=ctx.voucher_no,
        fields=tuple(fields),
        sections=tuple(sections),
        totals=(
            ("Gross earnings", total_of("gross", "earning")),
            ("Total deductions", total_of("withheld", "withheld")),
            ("Net pay", total_of("net", "earning")),
        ),
        footnotes=(
            "Year-to-date figures window by the book's fiscal year (ADR-012).",
            "Employer contributions book an expense and a liability; they never change net pay.",
            "Computer-generated from immutable posted entries (Law 2) — reprints are identical.",
        ),
        filename=f"Payslip {requested} {period}".replace("  ", " "),
    )


# ------------------------------------------------------------ master helpers
def _ensure_employee_ledger(
    resolver: Resolver, employee: str, override: str | None, group_override: str | None
):
    """The employee's payable ledger: default is the employee's own name
    under *Provisions*; an explicit ``payable_ledger`` override wins. The
    auto path refuses to silently reuse an unrelated existing ledger."""
    group_name = group_override or DEFAULT_PAYABLE_GROUP
    name = override or employee
    if override is None:
        existing = resolver.ledger(name=name)
        if existing is not None:
            payable_group = resolver.group(group_name)
            if payable_group is None or existing.group_id != payable_group.id:
                raise ValidationError(
                    f"payroll: employee {employee!r} collides with the existing ledger "
                    f"{name!r} (not under {group_name!r}) — set payable_ledger explicitly"
                )
    return resolver.ensure_ledger(name, group_name)


def _reject_conflicts(draft: VoucherDraft) -> None:
    for other in ("gst", "invoice", "receipt", "stock"):
        if draft.meta.get(other) is not None:
            raise ValidationError(
                f"payroll: meta.payroll and meta.{other} are mutually exclusive "
                "(post the documents separately)"
            )
    if any(ln.currency is not None for ln in draft.lines):
        raise ValidationError(
            "payroll: meta.payroll is not supported on foreign-currency lines "
            "(post FX vouchers separately)"
        )


def _reference_note(doc: dict) -> str:
    reference = str(doc.get("reference") or "").strip()
    return f"; {reference}" if reference else ""


# --------------------------------------------------------- the transformer
def build_payroll_document(draft: VoucherDraft, resolver: Resolver) -> VoucherDraft:
    doc = draft.meta.get(META_KEY)
    if doc is None:
        return draft
    if not isinstance(doc, dict):
        raise ValidationError("payroll: meta.payroll must be an object")

    mode = doc.get("mode")
    if mode == "run":
        return _build_run(draft, doc, resolver)
    if mode == "payout":
        return _build_payout(draft, doc, resolver)
    raise ValidationError(f"payroll: mode must be one of {', '.join(MODES_VALID)}, got {mode!r}")


def _validated_components(entry: dict, employee: str) -> list[tuple[str, str, Decimal, dict]]:
    """Parse + validate one employee's components → (name, kind, amount,
    raw). Component names are unique per employee: the same name under two
    kinds would resolve two different ledgers through one name."""
    components = entry.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError(f"payroll: employee {employee!r} needs a non-empty components list")
    parsed: list[tuple[str, str, Decimal, dict]] = []
    seen: set[str] = set()
    for i, comp in enumerate(components, start=1):
        if not isinstance(comp, dict):
            raise ValidationError(f"payroll: {employee} components[{i}] must be an object")
        name = str(comp.get("name") or "").strip()
        if not name:
            raise ValidationError(f"payroll: {employee} components[{i}].name is required")
        if name in seen:
            raise ValidationError(
                f"payroll: component {name!r} appears twice for {employee!r} "
                "(component names are unique per employee — merge the amounts)"
            )
        seen.add(name)
        kind = comp.get("kind")
        if kind not in KINDS_VALID:
            raise ValidationError(
                f"payroll: {employee} components[{i}].kind must be one of "
                f"{', '.join(KINDS_VALID)}, got {kind!r}"
            )
        amount = _decimal(comp.get("amount"), f"{employee} components[{i}].amount")
        if amount <= 0:
            raise ValidationError(f"payroll: {employee} components[{i}].amount must be > 0")
        if kind == "employer" and not _override(comp, "liability"):
            raise ValidationError(
                f"payroll: {employee} components[{i}] is an employer contribution — "
                "it requires a liability (the Cr side, e.g. PF Payable)"
            )
        parsed.append((name, kind, amount, comp))
    return parsed


def _build_run(draft: VoucherDraft, doc: dict, resolver: Resolver) -> VoucherDraft:
    _reject_conflicts(draft)

    year, month, period = _parse_period(doc.get("period"))
    # A run for a month that has not started is as much a fat-finger
    # candidate as a future date — same policy flag, same escape hatch.
    if date(year, month, 1) > date.today() and not draft.meta.get(
        "policy:allow_future_document_dates"
    ):
        raise ValidationError(
            f"Policy: payroll period {period} has not started — runs post for "
            "months already begun (override with "
            "policy:allow_future_document_dates=true)"
        )

    employees = doc.get("employees")
    if not isinstance(employees, list) or not employees:
        raise ValidationError("payroll: employees must be a non-empty list")

    names: list[str] = []
    parsed_by_employee: dict[str, list[tuple[str, str, Decimal, dict]]] = {}
    for i, entry in enumerate(employees, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"payroll: employees[{i}] must be an object")
        employee = str(entry.get("employee") or "").strip()
        if not employee:
            raise ValidationError(f"payroll: employees[{i}].employee is required")
        if employee in parsed_by_employee:
            raise ValidationError(f"payroll: employee {employee!r} appears twice in this run")
        parsed_by_employee[employee] = _validated_components(entry, employee)
        names.append(employee)

    # Serialize on every employee this run touches — one sorted pass, so
    # concurrent documents never deadlock (ADR-011 lock discipline).
    for name in sorted(set(names)):
        resolver.lock(f"payroll:{name}")

    documents = resolver.prior_documents(META_KEY)
    fy_start = resolver.fy_start_month
    fy = _fy_of(year, month, fy_start)
    states = {name: _fold(documents, name, fy=fy, upto=period, fy_start=fy_start) for name in names}

    if not draft.meta.get("policy:allow_duplicate_run"):
        for name in names:
            if _already_run(documents, name, period):
                raise ValidationError(
                    f"payroll: {name!r} already has a run for {period} "
                    "(reverse it, or override with policy:allow_duplicate_run=true "
                    "for a documented off-cycle correction)"
                )

    lines: list[DraftLine] = []
    computed_employees: list[dict] = []
    total_net = Decimal("0.00")

    for employee in names:
        entry = next(
            e
            for e in employees
            if isinstance(e, dict) and str(e.get("employee") or "").strip() == employee
        )
        components = parsed_by_employee[employee]
        state = states[employee]

        gross = sum((a for _, k, a, _ in components if k == "earning"), Decimal("0.00"))
        withheld = sum((a for _, k, a, _ in components if k == "withheld"), Decimal("0.00"))
        employer = sum((a for _, k, a, _ in components if k == "employer"), Decimal("0.00"))
        net = gross - withheld
        if net < 0:
            raise ValidationError(
                f"payroll: withheld exceeds gross for {employee!r} "
                f"({withheld} > {gross}) — recover the excess with a separate voucher, "
                "or correct the components"
            )

        employee_lines: list[DraftLine] = []
        if net > 0:
            payable_ledger = _ensure_employee_ledger(
                resolver,
                employee,
                _override(entry, "payable_ledger"),
                _override(entry, "payable_group"),
            )
            employee_lines.append(
                DraftLine(
                    dc="C",
                    amount=net,
                    ledger_id=payable_ledger.id,
                    ledger_name=payable_ledger.name,
                    narration=f"net pay — {employee} {period}{_reference_note(doc)}",
                )
            )
        for name, kind, amount, comp in components:
            if kind == "earning":
                ledger = resolver.ensure_ledger(name, _override(comp, "group") or EXPENSE_GROUP)
                employee_lines.append(
                    DraftLine(
                        dc="D",
                        amount=amount,
                        ledger_id=ledger.id,
                        ledger_name=ledger.name,
                        narration=f"{employee} — {name}",
                    )
                )
            elif kind == "withheld":
                ledger = resolver.ensure_ledger(name, _override(comp, "group") or STATUTORY_GROUP)
                employee_lines.append(
                    DraftLine(
                        dc="C",
                        amount=amount,
                        ledger_id=ledger.id,
                        ledger_name=ledger.name,
                        narration=f"{employee} — {name} (withheld)",
                    )
                )
            else:  # employer — an expense and a liability, never net pay
                expense = resolver.ensure_ledger(name, _override(comp, "group") or EXPENSE_GROUP)
                liability = resolver.ensure_ledger(
                    _override(comp, "liability") or name,
                    _override(comp, "liability_group") or STATUTORY_GROUP,
                )
                employee_lines.append(
                    DraftLine(
                        dc="D",
                        amount=amount,
                        ledger_id=expense.id,
                        ledger_name=expense.name,
                        narration=f"{employee} — {name}",
                    )
                )
                employee_lines.append(
                    DraftLine(
                        dc="C",
                        amount=amount,
                        ledger_id=liability.id,
                        ledger_name=liability.name,
                        narration=f"{employee} — {name} (employer)",
                    )
                )
        lines.extend(employee_lines)
        total_net += net

        computed_components = [
            {
                "name": name,
                "kind": kind,
                "amount": str(amount),
                "ytd": str(state.components.get((name, kind), Decimal("0.00")) + amount),
            }
            for name, kind, amount, _ in components
        ]
        computed_employees.append(
            {
                "employee": employee,
                "gross": str(gross),
                "withheld": str(withheld),
                "employer": str(employer),
                "net": str(net),
                "ytd": {
                    "gross": str(state.gross + gross),
                    "withheld": str(state.withheld + withheld),
                    "employer": str(state.employer + employer),
                    "net": str(state.net + net),
                    "runs": state.runs + 1,
                },
                "components": computed_components,
            }
        )

    if len(lines) < 2:
        raise ValidationError("payroll: the document produced fewer than 2 entries")
    doc["computed"] = {
        "period": period,
        "fy": str(fy),
        "employees": computed_employees,
        "total_net": str(total_net),
    }
    return draft.with_lines(lines)


def _build_payout(draft: VoucherDraft, doc: dict, resolver: Resolver) -> VoucherDraft:
    _reject_conflicts(draft)

    employee = str(doc.get("employee") or "").strip()
    if not employee:
        raise ValidationError("payroll: payout.employee is required")
    amount = _decimal(doc.get("amount"), "payout.amount")
    if amount <= 0:
        raise ValidationError("payroll: payout.amount must be > 0")
    bank_name = str(doc.get("bank") or "").strip()
    if not bank_name:
        raise ValidationError("payroll: payout.bank is required")

    resolver.lock(f"payroll:{employee}")
    documents = resolver.prior_documents(META_KEY)
    if not _has_relationship(documents, employee):
        raise ValidationError(
            f"payroll: {employee!r} has no payroll relationship in this book — "
            "post a run first, or pay a non-employee through a plain payment voucher"
        )

    state = _fold(documents, employee)
    position = state.net - state.paid
    if amount > position and not draft.meta.get("policy:allow_overpay"):
        raise ValidationError(
            f"payroll: payout exceeds {employee!r}'s payable: have {position}, "
            f"paying {amount} (an overpay books a recoverable advance — override "
            "with policy:allow_overpay=true)"
        )

    payable_ledger = _ensure_employee_ledger(
        resolver, employee, _override(doc, "payable_ledger"), _override(doc, "payable_group")
    )
    bank = resolver.ensure_ledger(bank_name, BANK_GROUP)
    note = f"salary payout — {employee}{_reference_note(doc)}"
    doc["computed"] = {"payable_after": str(position - amount)}
    return draft.with_lines(
        [
            DraftLine(
                dc="D",
                amount=amount,
                ledger_id=payable_ledger.id,
                ledger_name=payable_ledger.name,
                narration=note,
            ),
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=bank.id,
                ledger_name=bank.name,
                narration=note,
            ),
        ]
    )


# ------------------------------------------------------------ validators
def no_future_document_dates(draft: VoucherDraft, resolver: Resolver) -> None:
    if draft.meta.get(META_KEY) is None:
        return
    if draft.meta.get("policy:allow_future_document_dates"):
        return
    if draft.voucher_date > date.today():
        raise ValidationError(
            f"Policy: {draft.voucher_date.isoformat()} is in the future — payroll "
            "documents post on or before today (override with "
            "policy:allow_future_document_dates=true)"
        )


# --------------------------------------------------------- event handler
def record_run(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    doc = (event.data.get("meta") or {}).get(META_KEY)
    if not isinstance(doc, dict):
        return
    if doc.get("mode") == "run":
        RUNS.append(
            {
                "kind": "run",
                "voucher_no": event.data.get("voucher_no"),
                "book_id": event.data.get("book_id"),
                "period": doc.get("period"),
                "employees": [
                    str(e.get("employee"))
                    for e in doc.get("employees") or []
                    if isinstance(e, dict)
                ],
            }
        )
        logger.info("payroll run posted: %s (%s)", event.data.get("voucher_no"), doc.get("period"))
    elif doc.get("mode") == "payout":
        RUNS.append(
            {
                "kind": "payout",
                "voucher_no": event.data.get("voucher_no"),
                "book_id": event.data.get("book_id"),
                "employee": doc.get("employee"),
                "amount": str(doc.get("amount")),
            }
        )
        logger.info("salary payout posted: %s", event.data.get("voucher_no"))
