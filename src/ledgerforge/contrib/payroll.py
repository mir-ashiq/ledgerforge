"""Payroll plugin pack: salaries as documents on the voucher store (ADR-012),
deepened by the payroll depth (ADR-029): attendance-derived pay, loans &
advances, the full & final settlement, and the payslip outbox.

The roadmap's promise (analysis §9) is that payroll is "a plugin on the same
hook taxonomy + document store; the v0.7 read/lock primitives are its
building blocks" — this pack is that promise kept, the third business pack
after invoicing and inventory, landing with **zero schema change** in v0.8,
then keeping the promise a second time in v0.26: everything below the
attendance facts is still documents on ``meta.payroll``.

Four document modes, all on ``meta.payroll``:

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

  Net pay = Σ earnings − Σ withheld − Σ recoveries, credited to the
  employee's payable ledger (default: the employee's name under
  *Provisions*). One voucher carries the whole run — the TallyPrime
  payroll journal shape. An ``earning`` component may carry ``per_day``
  instead of ``amount``: attendance-derived pay — the amount is
  ``per_day × payable days`` of the period's live attendance register
  (plugin API 0.14), computed at post and recorded with its derivation.
  A ``recovery`` component credits the employee's advance ledger (a
  loan coming home), not a statutory liability: it reduces net pay and
  the loan outstanding, guarded by the outstanding fold. With a ``loan``
  reference and no amount it adopts the loan's scheduled EMI for the
  period — the automatic recovery TallyPrime's loan pay heads know.

- ``mode: "payout"`` — salary settlement: Dr the employee's payable, Cr the
  bank (under *Bank Accounts*). The invoicing pack's receipt, mirrored.

- ``mode: "loan"`` (ADR-029) — a staff loan or advance issued: Dr the
  employee's advance ledger (default: ``'{employee} Advance'`` under
  *Loans & Advances (Asset)* — a distinct name, because the payable
  ledger already holds the employee's own) / Cr the bank. The exact-Decimal
  annuity EMI schedule (``principal``, annual ``rate`` — 0 for an
  interest-free advance — ``months``, ``first_period``) is computed into
  ``computed`` and stays **advisory**: the schedule never books anything;
  recovery is a per-run fact. ``loan_position(resolver, employee)`` is the
  public fold — issued, recovered, outstanding, all-time, reversal-netted.

- ``mode: "settlement"`` (ADR-029) — the full & final settlement, one
  voucher: Dr the leave-encashment expense (``days × per_day`` — the
  operator's facts; a leave *balance* needs an accrual policy this pack
  refuses to invent), Dr extra earnings, Cr withheld liabilities, Cr each
  recovery to the advance ledger, and the net dues to the employee's
  payable — or straight to the bank when ``bank`` is present. After a
  settlement the employee is **relieved**: runs, loans and further
  settlements for them 422 (``policy:allow_post_settlement`` for the
  genuinely-late correction); payouts stay legal — paying the F&F dues is
  the whole point. A settlement's reversal reopens the relationship, the
  same pair-skipping walk every other fold runs.

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
pack adds no rounding of its own (the EMI schedule's final row absorbs its
own rounding so principals sum to the principal exactly). Per-employee
statutory caps remain out of scope. The ``RUNS`` list (after-commit seam)
records every posted payroll document, and every run appends one
``lf-payroll-payslip/1`` envelope per employee to the ``LF_PAYROLL_OUTBOX``
JSONL — the queue ``lf-payslip-bridge`` ships PDFs (and, only when the
operator configures SMTP, emails) from. Core never sends mail; this pack
never opens a socket.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentContext,
    DocumentSection,
    DocumentView,
    DraftLine,
    PackReport,
    PluginManifest,
    Resolver,
    VoucherDraft,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.payroll")

PLUGIN = PluginManifest(name="payroll", version="0.4.0", api_compat="0.14")

META_KEY = "payroll"

# Seed groups (ledgerforge/seeding.py) — masters resolve/create through the
# book-scoped Resolver only, so everything stays per-book (ADR-009).
EXPENSE_GROUP = "Indirect Expenses"  # earnings + employer contributions
STATUTORY_GROUP = "Duties & Taxes"  # withheld + employer liabilities
DEFAULT_PAYABLE_GROUP = "Provisions"  # employee net-pay ledgers
ADVANCE_GROUP = "Loans & Advances (Asset)"  # staff loans & advances (ADR-029)
BANK_GROUP = "Bank Accounts"  # payout settlement
ENCASHMENT_LEDGER = "Leave Encashment"  # the F&F encashment expense (ADR-029)

#: Every payroll document posted while this pack is loaded (observability
#: for tests/demos; the real-world consumer is a payslip/PF-filing bridge).
RUNS: list[dict] = []

CENT = Decimal("0.01")
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
MODES_VALID = ("run", "payout", "loan", "settlement")
KINDS_VALID = ("earning", "withheld", "employer", "recovery")


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_transformer(build_payroll_document)
    registry.register_validator(no_future_document_dates)
    registry.on("voucher.posted", record_run)
    registry.on("voucher.posted", queue_payslips)
    registry.register_document_view(META_KEY, payslip_view)
    registry.register_report("payroll-attendance", attendance_report)
    registry.register_report("payroll-loans", loans_report)


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
    also fills ``paid`` — the payable side — and the loan counters. Same
    fold, two windows: an accumulator windows by fiscal year, a balance
    never expires."""

    gross: Decimal = Decimal("0.00")
    withheld: Decimal = Decimal("0.00")
    employer: Decimal = Decimal("0.00")
    net: Decimal = Decimal("0.00")
    paid: Decimal = Decimal("0.00")
    runs: int = 0
    components: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    # ------------------------------------------------ ADR-029 (the depth)
    recovered: Decimal = Decimal("0.00")  # Σ recovery components, all-time
    loan_issued: Decimal = Decimal("0.00")  # Σ loan principals, all-time
    settlements: int = 0  # live F&F settlements (0 = the relationship is open)
    loans: dict[str, dict] = field(default_factory=dict)  # voucher_no → facts


def _entry_arithmetic(components: list) -> tuple[Decimal, Decimal, Decimal, Decimal, dict]:
    """One employee entry's (gross, withheld, employer, recovered,
    per-loan-recovered) from its raw component list — the arithmetic every
    mode shares (ADR-029: a recovery is net-reducing like a withheld, but
    it credits an advance, not a liability). Per-loan buckets key on the
    recovery's ``loan`` reference; unlinked recoveries land in ``""``."""
    gross = withheld = employer = recovered = Decimal("0.00")
    by_loan: dict[str, Decimal] = {}
    for comp in components:
        if not isinstance(comp, dict):
            continue
        kind = str(comp.get("kind") or "")
        try:
            amount = _decimal(comp.get("amount"), "amount")
        except ValidationError:
            continue
        if kind == "earning":
            gross += amount
        elif kind == "withheld":
            withheld += amount
        elif kind == "employer":
            employer += amount
        elif kind == "recovery":
            recovered += amount
            ref = str(comp.get("loan") or "").strip()
            by_loan[ref] = by_loan.get(ref, Decimal("0.00")) + amount
    return gross, withheld, employer, recovered, by_loan


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
    marks its original, and both are skipped). With ``fy`` set, only runs
    and settlements of that fiscal year (computed against ``fy_start``, the
    book's setting) with period <= ``upto`` accumulate — the YTD window;
    payouts and loans are always all-time (they settle balances, not
    accruals). Loan recoveries ride the run/settlement entries that carry
    them (ADR-029): ``state.recovered`` totals them, ``state.loans`` carries
    each loan's facts and its linked-recovery total."""
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
                (
                    entry_gross,
                    entry_withheld,
                    entry_employer,
                    entry_recovered,
                    by_loan,
                ) = _entry_arithmetic(entry.get("components") or [])
                for comp in entry.get("components") or []:
                    if not isinstance(comp, dict):
                        continue
                    kind = str(comp.get("kind") or "")
                    try:
                        amount = _decimal(comp.get("amount"), "amount")
                    except ValidationError:
                        continue
                    key = (str(comp.get("name") or ""), kind)
                    state.components[key] = state.components.get(key, Decimal("0.00")) + amount
                state.gross += entry_gross
                state.withheld += entry_withheld
                state.employer += entry_employer
                state.recovered += entry_recovered
                state.net += entry_gross - entry_withheld - entry_recovered
                state.runs += 1
                _apply_recoveries(state, by_loan)
        elif mode == "settlement" and str(doc.get("employee") or "").strip() == employee:
            try:
                year, month, _ = _parse_period(doc.get("period"))
            except ValidationError:
                continue  # a document this pack did not shape — not ours
            if fy is not None:
                if _fy_of(year, month, fy_start) != fy:
                    continue
                if upto is not None and f"{year:04d}-{month:02d}" > upto:
                    continue
            computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
            try:
                encashment = _decimal(
                    (computed.get("encashment") or {}).get("amount", 0), "encashment"
                )
            except ValidationError:
                encashment = Decimal("0.00")
            (
                s_gross,
                s_withheld,
                _s_employer,
                s_recovered,
                by_loan,
            ) = _entry_arithmetic(_settlement_components(doc))
            state.gross += encashment + s_gross
            state.withheld += s_withheld
            state.recovered += s_recovered
            state.net += encashment + s_gross - s_withheld - s_recovered
            state.settlements += 1
            _apply_recoveries(state, by_loan)
        elif mode == "loan" and str(doc.get("employee") or "").strip() == employee:
            try:
                principal = _decimal(doc.get("principal"), "principal")
            except ValidationError:
                continue
            state.loan_issued += principal
            computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
            state.loans[d.voucher_no] = {
                "voucher_no": d.voucher_no,
                "voucher_date": d.voucher_date.isoformat() if d.voucher_date else None,
                "principal": principal,
                "rate": str(doc.get("rate") or "0"),
                "months": doc.get("months"),
                "first_period": computed.get("first_period") or doc.get("first_period"),
                "emi": computed.get("emi"),
                "schedule": computed.get("schedule") or [],
                "advance_ledger": computed.get("advance_ledger"),
                "recovered": Decimal("0.00"),
            }
        elif mode == "payout" and str(doc.get("employee") or "").strip() == employee:
            try:
                state.paid += _decimal(doc.get("amount"), "amount")
            except ValidationError:
                continue
    return state


def _apply_recoveries(state: _EmployeeState, by_loan: dict[str, Decimal]) -> None:
    """Fold one entry's recovery buckets into the running state: linked
    recoveries reduce their named loan's outstanding; unlinked ones reduce
    the aggregate only (the guard's two ceilings, mirrored here)."""
    for ref, amount in by_loan.items():
        if not ref:
            continue
        loan = state.loans.get(ref)
        if loan is not None:
            loan["recovered"] = _decimal(loan["recovered"], "recovered") + amount


def _settlement_components(doc: dict) -> list[dict]:
    """The settlement document's earnings/withheld/recoveries as one
    component list (kinds assigned) — the shape ``_entry_arithmetic`` and
    the ledger walk share."""
    out: list[dict] = []
    pairs = (
        ("earning", "earnings"),
        ("withheld", "withheld"),
        ("recovery", "recoveries"),
    )
    for kind, key in pairs:
        for item in doc.get(key) or []:
            if isinstance(item, dict):
                out.append({**item, "kind": kind})
    return out


def _is_settled_doc(doc: dict, employee: str) -> bool:
    """One live settlement naming the employee? (ADR-029: the relieved
    gate's atom — a settlement's reversal reopens the relationship.)"""
    return doc.get("mode") == "settlement" and str(doc.get("employee") or "").strip() == employee


def _is_settled_documents(documents: list, employee: str) -> bool:
    """The relieved gate over the document sequence — reversed settlements
    don't count, so undoing an F&F reopens the relationship exactly as
    undoing a run restores YTD (the ADR-010/011 pair walk)."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if isinstance(doc, dict) and _is_settled_doc(doc, employee):
            return True
    return False


def _has_relationship(documents: list, employee: str) -> bool:
    """Any non-reversed payroll document naming the employee — the gate for
    payouts (a payout settles a payroll relationship; a payment to a
    stranger is a plain payment voucher). ADR-029 widens it: a loan issue
    and a settlement are payroll documents too — the relationship begins
    with the first of them, whatever it was."""
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict):
            continue
        mode = doc.get("mode")
        if mode in ("payout", "loan") and str(doc.get("employee") or "").strip() == employee:
            return True
        if mode == "run" and any(True for _ in _iter_employee_runs(doc, employee)):
            return True
        if mode == "settlement" and _is_settled_doc(doc, employee):
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
    view (PF/ESI wage bases, Form-16 style totals). ``recovered`` (ADR-029)
    totals the loan recoveries that reduced net pay — a deduction record,
    never a slab."""

    gross: Decimal
    withheld: Decimal
    employer: Decimal
    net: Decimal
    runs: int
    recovered: Decimal = Decimal("0.00")


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
        recovered=state.recovered,
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


# ---------------------------------------------- ADR-029 public derived views
@dataclass(frozen=True)
class LoanPosition:
    """One employee's all-time loan & advance position — issued minus
    recovered, reversal-netted. ``by_loan`` carries each live loan's own
    outstanding (linked recoveries only); the aggregate outstanding nets
    unlinked recoveries too — the guard's two ceilings, one view."""

    issued: Decimal
    recovered: Decimal
    outstanding: Decimal
    by_loan: dict[str, Decimal]


def loan_position(resolver: Resolver, employee: str) -> LoanPosition:
    """The employee's loan & advance position in the resolver's book — the
    same fold the recovery guard runs, so a run can never recover more
    than the books say is outstanding."""
    state = _fold(resolver.prior_documents(META_KEY), employee)
    by_loan = {no: loan["principal"] - loan["recovered"] for no, loan in state.loans.items()}
    return LoanPosition(
        issued=state.loan_issued,
        recovered=state.recovered,
        outstanding=state.loan_issued - state.recovered,
        by_loan=by_loan,
    )


def is_settled(resolver: Resolver, employee: str) -> bool:
    """Does a live full & final settlement close this employee's payroll
    relationship? (ADR-029 — the relieved gate; a settlement's reversal
    reopens it, exactly like a run's reversal restores YTD.)"""
    return _is_settled_documents(resolver.prior_documents(META_KEY), employee)


def payable_days(row) -> Decimal:
    """One attendance row's payable days (ADR-029) — the pack's own
    arithmetic over the kernel's raw counts: present + half × 0.5 +
    leave-with-pay. An absent day is unpaid; a half-day pays half."""
    return row.present + row.half / 2 + row.leave_paid


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

    Runs claim the payslip; settlements (ADR-029) claim the full & final
    statement. A run is one voucher for many employees, so the caller
    picks the subject with ``?employee=<name>`` (the sole employee of a
    single-employee run is the default). Every figure is the pack's own:
    per-component amounts and YTD from the voucher's immutable
    ``computed`` block, the payable position replayed through the same
    ``payable()`` fold the payout guard runs — a payslip that disagrees
    with the ledger is impossible by construction. Payouts and loans
    decline (``None``) and print as the voucher slip.
    """
    doc = ctx.meta.get(META_KEY)
    if not isinstance(doc, dict) or doc.get("mode") not in ("run", "settlement"):
        return None
    if doc.get("mode") == "settlement":
        return settlement_view(ctx)

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


def _ensure_advance_ledger(
    resolver: Resolver, employee: str, override: str | None, group_override: str | None
):
    """The employee's advance ledger (ADR-029): default is ``'{employee}
    Advance'`` under *Loans & Advances (Asset)* — a distinct name, because
    ledgers are unique per book and the employee's payable ledger already
    holds the plain name (ADR-012's default). An explicit override wins —
    and a default that lands on an existing ledger outside the group
    refuses, the payable path's guard."""
    group_name = group_override or ADVANCE_GROUP
    name = override or f"{employee} Advance"
    if override is None:
        existing = resolver.ledger(name=name)
        if existing is not None:
            advance_group = resolver.group(group_name)
            if advance_group is None or existing.group_id != advance_group.id:
                raise ValidationError(
                    f"payroll: employee {employee!r} collides with the existing ledger "
                    f"{name!r} (not under {group_name!r}) — set advance_ledger explicitly"
                )
    return resolver.ensure_ledger(name, group_name)


# --------------------------------------------------------- the EMI schedule
def _next_period(period: str) -> str:
    """One month after ``YYYY-MM`` — the schedule's clock."""
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def emi_schedule(
    principal: Decimal, rate: Decimal, months: int, first_period: str
) -> tuple[Decimal, list[dict]]:
    """The exact-Decimal annuity schedule (ADR-029): the EMI
    (``P·r·(1+r)^n / ((1+r)^n − 1)``, ``P/n`` when the rate is 0 — an
    interest-free advance), each row's interest quantized at the monthly
    rate and the **final row absorbing the rounding** so the principals
    sum to the principal exactly. Returns ``(emi, rows)`` with every row
    ``{period, opening, interest, principal, emi, closing}`` — figures a
    payslip or a loan statement can print as-is."""
    if months < 1:
        raise ValidationError("payroll: loan.months must be >= 1")
    monthly_rate = rate / Decimal(100) / Decimal(12)
    if monthly_rate == 0:
        emi = _money(principal / Decimal(months))
    else:
        factor = (Decimal(1) + monthly_rate) ** months
        emi = _money(principal * monthly_rate * factor / (factor - Decimal(1)))

    rows: list[dict] = []
    opening = principal
    period = first_period
    for i in range(1, months + 1):
        interest = _money(opening * monthly_rate)
        if i == months:  # the closing row absorbs the accumulated rounding
            principal_part = opening
            emi_i = _money(principal_part + interest)
        else:
            principal_part = emi - interest
            emi_i = emi
        closing = opening - principal_part
        rows.append(
            {
                "period": period,
                "opening": str(opening),
                "interest": str(interest),
                "principal": str(principal_part),
                "emi": str(emi_i),
                "closing": str(closing),
            }
        )
        opening = closing
        period = _next_period(period)
    return emi, rows


def _scheduled_emi(
    state: _EmployeeState, loan_ref: str, period: str, employee: str, component: str
) -> Decimal:
    """The auto-recovery amount (ADR-029): the named loan's scheduled EMI
    for the run's period. The loan must exist, belong to this employee and
    carry a schedule row for the period — an installment that is not due
    is never adopted (guessing is the pack's one unforgivable)."""
    loan = state.loans.get(loan_ref)
    if loan is None:
        raise ValidationError(
            f"payroll: {employee!r}'s recovery {component!r} names loan {loan_ref!r}, "
            "which is not a live loan document of theirs — check the voucher number"
        )
    for row in loan["schedule"]:
        if str(row.get("period")) == period:
            return _decimal(row.get("emi"), "emi")
    schedule = loan["schedule"] or [{}]
    first = schedule[0].get("period", "?")
    last = schedule[-1].get("period", "?")
    raise ValidationError(
        f"payroll: loan {loan_ref!r} has no installment due in {period} for "
        f"{employee!r}'s recovery {component!r} — the schedule runs "
        f"{first}…{last}; "
        "send an explicit amount for an off-schedule recovery"
    )


def _guard_recoveries(
    components: list[tuple[str, str, Decimal, dict]], employee: str, state: _EmployeeState
) -> None:
    """The recovery ceilings (ADR-029): this document's recoveries may not
    cross the employee's aggregate outstanding, and a named loan's
    recoveries may not cross that loan's own. Prior documents' recoveries
    already sit in the fold — the ceilings are fold-checked, so two
    concurrent runs cannot both spend the same outstanding (the lock
    serializes them)."""
    if not any(k == "recovery" for _, k, _, _ in components):
        return
    position = loan_position_from_state(state)
    total = sum((a for _, k, a, _ in components if k == "recovery"), Decimal("0.00"))
    if total > position.outstanding:
        raise ValidationError(
            f"payroll: recoveries for {employee!r} exceed their loan outstanding: "
            f"recovering {total}, outstanding {position.outstanding} — recover less, "
            "or top the loan up first"
        )
    per_loan: dict[str, Decimal] = {}
    for name, kind, amount, comp in components:
        if kind != "recovery":
            continue
        ref = _override(comp, "loan")
        if ref is None:
            continue
        if ref not in state.loans:
            raise ValidationError(
                f"payroll: {employee!r}'s recovery {name!r} names loan {ref!r}, which is "
                "not a live loan document of theirs — check the voucher number"
            )
        per_loan[ref] = per_loan.get(ref, Decimal("0.00")) + amount
    for ref, amount in per_loan.items():
        ceiling = position.by_loan[ref]
        if amount > ceiling:
            raise ValidationError(
                f"payroll: recoveries against loan {ref!r} for {employee!r} exceed its "
                f"outstanding: recovering {amount}, outstanding {ceiling}"
            )


def loan_position_from_state(state: _EmployeeState) -> LoanPosition:
    """The loan position from an already-folded state (the guard's path —
    ``loan_position`` is the same arithmetic over a fresh fold)."""
    by_loan = {no: loan["principal"] - loan["recovered"] for no, loan in state.loans.items()}
    return LoanPosition(
        issued=state.loan_issued,
        recovered=state.recovered,
        outstanding=state.loan_issued - state.recovered,
        by_loan=by_loan,
    )


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
    if mode == "loan":
        return _build_loan(draft, doc, resolver)
    if mode == "settlement":
        return _build_settlement(draft, doc, resolver)
    raise ValidationError(f"payroll: mode must be one of {', '.join(MODES_VALID)}, got {mode!r}")


def _validated_components(
    entry: dict, employee: str
) -> list[tuple[str, str, Decimal | None, dict]]:
    """Parse + validate one employee's components → (name, kind, amount,
    raw). Component names are unique per employee: the same name under two
    kinds would resolve two different ledgers through one name. The amount
    may be absent for exactly two kinds (ADR-029): an ``earning`` with
    ``per_day`` (attendance-derived — resolved against the live register)
    and a ``recovery`` with a ``loan`` reference (the scheduled EMI —
    resolved against the loan document). Everything else needs its exact
    figure; the pack never guesses an amount."""
    components = entry.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError(f"payroll: employee {employee!r} needs a non-empty components list")
    parsed: list[tuple[str, str, Decimal | None, dict]] = []
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
        amount: Decimal | None = None
        if comp.get("amount") is not None:
            amount = _decimal(comp.get("amount"), f"{employee} components[{i}].amount")
            if amount <= 0:
                raise ValidationError(f"payroll: {employee} components[{i}].amount must be > 0")
        per_day = (
            _decimal(comp.get("per_day"), f"{employee} components[{i}].per_day")
            if comp.get("per_day") is not None
            else None
        )
        if kind == "earning":
            if per_day is not None:
                if amount is not None:
                    raise ValidationError(
                        f"payroll: {employee} components[{i}] ({name!r}) carries both amount "
                        "and per_day — attendance-derived pay is per_day × payable days, "
                        "never both (send one or the other)"
                    )
                if per_day <= 0:
                    raise ValidationError(
                        f"payroll: {employee} components[{i}].per_day must be > 0"
                    )
            elif amount is None:
                raise ValidationError(
                    f"payroll: {employee} components[{i}] ({name!r}) needs an amount "
                    "or per_day (attendance-derived pay)"
                )
        elif kind == "recovery":
            loan_ref = _override(comp, "loan")
            if amount is None and loan_ref is None:
                raise ValidationError(
                    f"payroll: {employee} components[{i}] is a recovery — it needs an "
                    "amount, or a loan reference to adopt that loan's scheduled EMI"
                )
        elif amount is None:
            raise ValidationError(f"payroll: {employee} components[{i}] ({name!r}) needs an amount")
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

    # ADR-029: a settled employee is relieved — runs, loans and further
    # settlements refuse; payouts stay legal (the dues are the point).
    if not draft.meta.get("policy:allow_post_settlement"):
        for name in names:
            if _is_settled_documents(documents, name):
                raise ValidationError(
                    f"payroll: {name!r} is relieved — a full & final settlement closed "
                    "their payroll relationship (override with "
                    "policy:allow_post_settlement=true for a genuinely-late correction)"
                )

    if not draft.meta.get("policy:allow_duplicate_run"):
        for name in names:
            if _already_run(documents, name, period):
                raise ValidationError(
                    f"payroll: {name!r} already has a run for {period} "
                    "(reverse it, or override with policy:allow_duplicate_run=true "
                    "for a documented off-cycle correction)"
                )

    # Resolve the two derived amounts (ADR-029) — per-day pay against the
    # live register (one read for the whole run) and the auto-EMI against
    # the loan documents. The resolved figure is written back into the
    # component dict, so the stored document carries the exact amount it
    # posted — the fold replays figures, never derivations.
    register: dict[str, Any] = {}
    wants_attendance = any(
        comp.get("per_day") is not None
        for parsed in parsed_by_employee.values()
        for _, _, _, comp in parsed
    )
    if wants_attendance:
        register = {row.employee: row for row in resolver.attendance_lines(period)}

    for employee in names:
        for cname, ckind, camount, comp in parsed_by_employee[employee]:
            if ckind == "earning" and camount is None:  # per_day × payable days
                row = register.get(employee)
                if row is None:
                    raise ValidationError(
                        f"payroll: {employee!r}'s component {cname!r} is attendance-derived "
                        f"(per_day) but the {period} register has no row for them — import "
                        "the register first (POST /api/v1/payroll/attendance)"
                    )
                days = _money(payable_days(row))
                if days <= 0:
                    raise ValidationError(
                        f"payroll: {employee!r} has 0 payable days in the {period} register "
                        f"— post the run without {cname!r} instead of paying for no days"
                    )
                resolved = _money(_decimal(comp.get("per_day"), "per_day") * days)
                comp["amount"] = str(resolved)
                comp["days"] = str(days)
            elif ckind == "recovery" and camount is None:  # the scheduled EMI
                loan_ref = str(comp.get("loan") or "").strip()
                emi = _scheduled_emi(states[employee], loan_ref, period, employee, cname)
                comp["amount"] = str(emi)

    lines: list[DraftLine] = []
    computed_employees: list[dict] = []
    total_net = Decimal("0.00")

    for employee in names:
        entry = next(
            e
            for e in employees
            if isinstance(e, dict) and str(e.get("employee") or "").strip() == employee
        )
        components = [
            (n, k, _decimal(c.get("amount"), "amount"), c)
            for n, k, _a, c in parsed_by_employee[employee]
        ]
        state = states[employee]

        gross = sum((a for _, k, a, _ in components if k == "earning"), Decimal("0.00"))
        withheld = sum((a for _, k, a, _ in components if k == "withheld"), Decimal("0.00"))
        employer = sum((a for _, k, a, _ in components if k == "employer"), Decimal("0.00"))
        recovered = sum((a for _, k, a, _ in components if k == "recovery"), Decimal("0.00"))
        net = gross - withheld - recovered
        if net < 0:
            raise ValidationError(
                f"payroll: withheld + recoveries exceed gross for {employee!r} "
                f"({withheld} + {recovered} > {gross}) — recover the excess with a "
                "separate voucher, or correct the components"
            )

        # The recovery ceilings (ADR-029): the aggregate never crosses the
        # employee's outstanding; a named loan's recoveries never cross that
        # loan's own. Two loans, one run, one guard each.
        _guard_recoveries(components, employee, state)

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
            elif kind == "recovery":
                # a loan coming home: credit the advance ledger, never a
                # statutory liability — the named loan's ledger when the
                # component names one, the employee's default otherwise.
                loan_ref = _override(comp, "loan")
                if loan_ref is not None:
                    loan = state.loans.get(loan_ref)
                    default_ledger = (loan or {}).get("advance_ledger") or employee
                    default_group = (loan or {}).get("advance_group")
                else:
                    default_ledger, default_group = employee, None
                ledger = _ensure_advance_ledger(
                    resolver,
                    employee,
                    _override(comp, "advance_ledger") or default_ledger,
                    _override(comp, "advance_group") or default_group,
                )
                employee_lines.append(
                    DraftLine(
                        dc="C",
                        amount=amount,
                        ledger_id=ledger.id,
                        ledger_name=ledger.name,
                        narration=f"{employee} — {name} (loan recovery)"
                        + (f" [{loan_ref}]" if loan_ref else ""),
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
                **({"days": comp["days"]} if kind == "earning" and "days" in comp else {}),
                **(
                    {"per_day": str(comp.get("per_day"))}
                    if kind == "earning" and comp.get("per_day") is not None
                    else {}
                ),
            }
            for name, kind, amount, comp in components
        ]
        computed_employees.append(
            {
                "employee": employee,
                "gross": str(gross),
                "withheld": str(withheld),
                "employer": str(employer),
                "recovered": str(recovered),
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


# ------------------------------------------------- ADR-029: the loan builder
def _build_loan(draft: VoucherDraft, doc: dict, resolver: Resolver) -> VoucherDraft:
    """``mode: "loan"`` — a staff loan or advance issued: Dr the employee's
    advance ledger / Cr the bank, with the exact-Decimal annuity schedule
    computed into ``computed`` (advisory — the schedule never books
    anything; recovery is a per-run fact)."""
    _reject_conflicts(draft)

    employee = str(doc.get("employee") or "").strip()
    if not employee:
        raise ValidationError("payroll: loan.employee is required")
    principal = _decimal(doc.get("principal"), "loan.principal")
    if principal <= 0:
        raise ValidationError("payroll: loan.principal must be > 0")
    rate = _decimal(doc.get("rate", 0), "loan.rate")
    if rate < 0:
        raise ValidationError(
            "payroll: loan.rate must be >= 0 (an annual percentage; 0 = interest-free)"
        )
    months = doc.get("months")
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        raise ValidationError(f"payroll: loan.months must be a whole number >= 1, got {months!r}")
    bank_name = str(doc.get("bank") or "").strip()
    if not bank_name:
        raise ValidationError("payroll: loan.bank is required")

    # The first installment's period: explicit, or the month after the
    # voucher's own month (a loan issued in November first recovers in
    # December — the payroll month the schedule names).
    if _override(doc, "first_period") is not None:
        _y, _m, first_period = _parse_period(doc.get("first_period"))
    else:
        first_period = _next_period(f"{draft.voucher_date.year:04d}-{draft.voucher_date.month:02d}")

    resolver.lock(f"payroll:{employee}")
    documents = resolver.prior_documents(META_KEY)

    # ADR-029: an advance to a relieved employee is a plain JV if it is
    # truly needed — the payroll pack refuses to reopen a closed
    # relationship by lending into it.
    if _is_settled_documents(documents, employee) and not draft.meta.get(
        "policy:allow_post_settlement"
    ):
        raise ValidationError(
            f"payroll: {employee!r} is relieved — a full & final settlement closed their "
            "payroll relationship; a post-settlement advance is a plain journal voucher "
            "(override with policy:allow_post_settlement=true if it is truly one)"
        )

    emi, schedule = emi_schedule(principal, rate, months, first_period)
    total_interest = sum(
        (_decimal(row.get("interest"), "interest") for row in schedule), Decimal("0.00")
    )

    advance = _ensure_advance_ledger(
        resolver, employee, _override(doc, "advance_ledger"), _override(doc, "advance_group")
    )
    bank = resolver.ensure_ledger(bank_name, BANK_GROUP)
    note = f"staff loan issued — {employee}{_reference_note(doc)}"
    doc["computed"] = {
        "emi": str(emi),
        "months": months,
        "rate": str(rate),
        "first_period": first_period,
        "total_interest": str(total_interest),
        "schedule": schedule,
        "advance_ledger": advance.name,
        "advance_group": _override(doc, "advance_group"),
        "outstanding": str(principal),
    }
    return draft.with_lines(
        [
            DraftLine(
                dc="D",
                amount=principal,
                ledger_id=advance.id,
                ledger_name=advance.name,
                narration=note,
            ),
            DraftLine(
                dc="C",
                amount=principal,
                ledger_id=bank.id,
                ledger_name=bank.name,
                narration=note,
            ),
        ]
    )


# ------------------------------------------- ADR-029: the settlement builder
def _build_settlement(draft: VoucherDraft, doc: dict, resolver: Resolver) -> VoucherDraft:
    """``mode: "settlement"`` — the full & final settlement, one voucher:
    leave encashment expensed (days × per_day), extra earnings, withheld
    liabilities, recoveries to the advance ledger, and the net dues to the
    payable ledger — or straight to the bank when ``bank`` is present."""
    _reject_conflicts(draft)

    employee = str(doc.get("employee") or "").strip()
    if not employee:
        raise ValidationError("payroll: settlement.employee is required")
    year, month, period = _parse_period(doc.get("period"))

    encashment_doc = doc.get("encashment")
    encashment = Decimal("0.00")
    enc_days: Decimal | None = None
    enc_per_day: Decimal | None = None
    if encashment_doc is not None:
        if not isinstance(encashment_doc, dict):
            raise ValidationError(
                "payroll: settlement.encashment must be an object {days, per_day}"
            )
        enc_days = _decimal(encashment_doc.get("days"), "settlement.encashment.days")
        enc_per_day = _decimal(encashment_doc.get("per_day"), "settlement.encashment.per_day")
        if enc_days < 0:
            raise ValidationError("payroll: settlement.encashment.days must be >= 0")
        if enc_days > 0:
            if enc_per_day is None or enc_per_day <= 0:
                raise ValidationError(
                    "payroll: settlement.encashment needs a per_day rate > 0 (the leave "
                    "balance is the operator's fact; this pack computes days × rate, "
                    "never the days themselves)"
                )
            encashment = _money(enc_days * enc_per_day)

    earnings = _validated_simple_list(doc, "earnings", "earning")
    withheld = _validated_simple_list(doc, "withheld", "withheld")
    recoveries = _validated_simple_list(doc, "recoveries", "recovery")

    resolver.lock(f"payroll:{employee}")
    documents = resolver.prior_documents(META_KEY)
    if not _has_relationship(documents, employee):
        raise ValidationError(
            f"payroll: {employee!r} has no payroll relationship in this book — "
            "a settlement closes one, it cannot open it"
        )
    state = _fold(documents, employee)

    # The settlement's own auto-EMI resolution (ADR-029): a recovery naming
    # its loan with no amount adopts the schedule's EMI for the settlement's
    # period — the final month's installment, recovered with the F&F.
    resolved_recoveries: list[tuple[str, Decimal, dict]] = []
    for name, amount, item in recoveries:
        if amount is None:
            loan_ref = str(item.get("loan") or "").strip()
            amount = _scheduled_emi(state, loan_ref, period, employee, name)
            item["amount"] = str(amount)
        resolved_recoveries.append((name, amount, item))
    recoveries = resolved_recoveries

    # One settlement per employee — the F&F's own duplicate-run guard.
    if not draft.meta.get("policy:allow_duplicate_settlement") and state.settlements > 0:
        raise ValidationError(
            f"payroll: {employee!r} already has a full & final settlement in this book "
            "(reverse it, or override with policy:allow_duplicate_settlement=true)"
        )

    gross = encashment + sum((a for _, a, _ in earnings), Decimal("0.00"))
    total_withheld = sum((a for _, a, _ in withheld), Decimal("0.00"))
    total_recovered = sum((a for _, a, _ in recoveries), Decimal("0.00"))
    net = gross - total_withheld - total_recovered
    if net < 0:
        raise ValidationError(
            f"payroll: withheld + recoveries exceed the settlement dues for {employee!r} "
            f"({total_withheld} + {total_recovered} > {gross}) — the employee would owe "
            "the company; recover the excess with a separate voucher"
        )

    components = [
        *[(n, "earning", a, c) for n, a, c in earnings],
        *[(n, "withheld", a, c) for n, a, c in withheld],
        *[(n, "recovery", a, c) for n, a, c in recoveries],
    ]
    _guard_recoveries(components, employee, state)

    lines: list[DraftLine] = []
    if encashment > 0:
        enc_ledger = resolver.ensure_ledger(
            _override(encashment_doc, "ledger") or ENCASHMENT_LEDGER,
            _override(encashment_doc, "group") or EXPENSE_GROUP,
        )
        lines.append(
            DraftLine(
                dc="D",
                amount=encashment,
                ledger_id=enc_ledger.id,
                ledger_name=enc_ledger.name,
                narration=f"{employee} — leave encashment ({enc_days} × {enc_per_day})",
            )
        )
    for name, amount, comp in earnings:
        ledger = resolver.ensure_ledger(name, _override(comp, "group") or EXPENSE_GROUP)
        lines.append(
            DraftLine(
                dc="D",
                amount=amount,
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"{employee} — {name} (F&F)",
            )
        )
    for name, amount, comp in withheld:
        ledger = resolver.ensure_ledger(name, _override(comp, "group") or STATUTORY_GROUP)
        lines.append(
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"{employee} — {name} (withheld, F&F)",
            )
        )
    for name, amount, comp in recoveries:
        loan_ref = _override(comp, "loan")
        if loan_ref is not None:
            loan = state.loans.get(loan_ref)
            default_ledger = (loan or {}).get("advance_ledger") or employee
            default_group = (loan or {}).get("advance_group")
        else:
            default_ledger, default_group = employee, None
        ledger = _ensure_advance_ledger(
            resolver,
            employee,
            _override(comp, "advance_ledger") or default_ledger,
            _override(comp, "advance_group") or default_group,
        )
        lines.append(
            DraftLine(
                dc="C",
                amount=amount,
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                narration=f"{employee} — {name} (loan recovery, F&F)"
                + (f" [{loan_ref}]" if loan_ref else ""),
            )
        )

    if net > 0:
        bank_name = _override(doc, "bank")
        if bank_name is not None:
            # the dues leave with the settlement itself — paid out direct
            settlement_ledger = resolver.ensure_ledger(bank_name, BANK_GROUP)
            narration = f"full & final settlement — {employee}{_reference_note(doc)}"
        else:
            settlement_ledger = _ensure_employee_ledger(
                resolver,
                employee,
                _override(doc, "payable_ledger"),
                _override(doc, "payable_group"),
            )
            narration = f"F&F net dues — {employee} {period}{_reference_note(doc)}"
        lines.append(
            DraftLine(
                dc="C",
                amount=net,
                ledger_id=settlement_ledger.id,
                ledger_name=settlement_ledger.name,
                narration=narration,
            )
        )

    if len(lines) < 2:
        raise ValidationError(
            "payroll: the settlement produced fewer than 2 entries — an empty F&F "
            "is a resignation letter, not a voucher"
        )

    position_after = state.net + net - state.paid
    doc["computed"] = {
        "period": period,
        "employee": employee,
        "encashment": {
            "days": str(enc_days) if enc_days is not None else None,
            "per_day": str(enc_per_day) if enc_per_day is not None else None,
            "amount": str(encashment),
        },
        "gross": str(gross),
        "withheld": str(total_withheld),
        "recovered": str(total_recovered),
        "net": str(net),
        "paid_direct": _override(doc, "bank") is not None,
        "payable_after": str(position_after),
        "loan_outstanding_after": str(state.loan_issued - state.recovered - total_recovered),
    }
    return draft.with_lines(lines)


def _validated_simple_list(doc: dict, key: str, kind: str) -> list[tuple[str, Decimal, dict]]:
    """A settlement's earnings/withheld/recoveries list: one {name, amount,
    loan?} object per line, names unique, amounts exact and positive. The
    recovery list may adopt a loan's scheduled EMI for the settlement's own
    period (``loan`` set, ``amount`` absent) — the F&F recovers what the
    schedule says the final month owes."""
    items = doc.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValidationError(f"payroll: settlement.{key} must be a list")
    parsed: list[tuple[str, Decimal, dict]] = []
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"payroll: settlement.{key}[{i}] must be an object")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValidationError(f"payroll: settlement.{key}[{i}].name is required")
        if name in seen:
            raise ValidationError(
                f"payroll: settlement.{key}[{i}] {name!r} appears twice — merge the amounts"
            )
        seen.add(name)
        amount: Decimal | None = None
        if item.get("amount") is not None:
            amount = _decimal(item.get("amount"), f"settlement.{key}[{i}].amount")
            if amount <= 0:
                raise ValidationError(f"payroll: settlement.{key}[{i}].amount must be > 0")
        elif kind == "recovery" and _override(item, "loan") is not None:
            amount = None  # the scheduled EMI — resolved by the caller
        else:
            raise ValidationError(f"payroll: settlement.{key}[{i}].amount is required")
        parsed.append((name, amount, item))  # type: ignore[arg-type]
    return parsed


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
    elif doc.get("mode") == "loan":
        RUNS.append(
            {
                "kind": "loan",
                "voucher_no": event.data.get("voucher_no"),
                "book_id": event.data.get("book_id"),
                "employee": doc.get("employee"),
                "principal": str(doc.get("principal")),
                "months": doc.get("months"),
            }
        )
        logger.info("staff loan issued: %s", event.data.get("voucher_no"))
    elif doc.get("mode") == "settlement":
        RUNS.append(
            {
                "kind": "settlement",
                "voucher_no": event.data.get("voucher_no"),
                "book_id": event.data.get("book_id"),
                "employee": doc.get("employee"),
                "period": doc.get("period"),
            }
        )
        logger.info("full & final settlement posted: %s", event.data.get("voucher_no"))


def queue_payslips(event) -> None:  # noqa: ANN001 — DomainEvent from the stable API
    """The payslip outbox's after-commit half (ADR-029): append one
    ``lf-payroll-payslip/1`` envelope per employee of a posted run to the
    ``LF_PAYROLL_OUTBOX`` JSONL file — the queue ``lf-payslip-bridge``
    renders (and, when the operator configures SMTP, emails) from.
    Out-of-process by contract (D3, the IRN/EWB precedent): the outbox is
    the only hand-off — no network I/O ever runs inside the posting path,
    and a missing/unwritable outbox is logged and isolated (an event
    handler must never break posting). Core never sends mail; this handler
    never opens a socket."""
    outbox = os.environ.get("LF_PAYROLL_OUTBOX", "").strip()
    if not outbox:
        return
    doc = (event.data.get("meta") or {}).get(META_KEY)
    if not isinstance(doc, dict) or doc.get("mode") != "run":
        return
    base = {
        "kind": "lf-payroll-payslip/1",
        "voucher_id": event.data.get("voucher_id"),
        "voucher_no": event.data.get("voucher_no"),
        "book_id": event.data.get("book_id"),
        "period": doc.get("period"),
        "queued_at": datetime.now(UTC).isoformat(),
    }
    entries = [
        e
        for e in (doc.get("employees") or [])
        if isinstance(e, dict) and str(e.get("employee") or "").strip()
    ]
    if not entries:
        return
    path = Path(outbox)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                envelope = {
                    **base,
                    "employee": str(entry.get("employee")).strip(),
                    "email": str(entry.get("email") or "").strip() or None,
                }
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        logger.info(
            "queued %d payslips of %s for distribution", len(entries), event.data.get("voucher_no")
        )
    except OSError as exc:  # isolated by contract — posting already succeeded
        logger.warning(
            "payslip outbox %s unwritable (%s) — %d payslips of %s not queued",
            outbox,
            exc,
            len(entries),
            event.data.get("voucher_no"),
        )


# ------------------------------------------------------ ADR-029 pack reports
def attendance_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The live attendance register of one period — JSON for the UI,
    CSV for the machine, PDF for the file: every employee's raw counts and
    the pack's payable-days arithmetic, the same rows a per-day run
    derives from (so the register a payslip prices is never a mystery)."""
    raw = str(params.get("period") or "").strip()
    year, month, period = _parse_period(raw or "")
    rows = resolver.attendance_lines(period)
    shaped = [
        {
            "employee": row.employee,
            "present": str(row.present),
            "absent": str(row.absent),
            "half": str(row.half),
            "leave_paid": str(row.leave_paid),
            "payable_days": str(_money(payable_days(row))),
            "note": row.note,
        }
        for row in rows
    ]
    fmt = str(params.get("format") or "").strip().lower()
    title = f"Attendance register {period}"
    if fmt == "csv":
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(
            ["employee", "present", "absent", "half", "leave_paid", "payable_days", "note"]
        )
        for row in shaped:
            writer.writerow(
                [
                    row["employee"],
                    row["present"],
                    row["absent"],
                    row["half"],
                    row["leave_paid"],
                    row["payable_days"],
                    row["note"] or "",
                ]
            )
        return PackReport(title=title, filename=f"Attendance {period}", csv=buf.getvalue())
    if fmt == "json":
        return PackReport(
            title=title, filename=f"Attendance {period}", data={"period": period, "rows": shaped}
        )
    sections = [
        DocumentSection(
            title="The register",
            columns=(
                DocumentColumn("Employee", width=2.4),
                DocumentColumn("Present", width=0.8, align="right"),
                DocumentColumn("Absent", width=0.8, align="right"),
                DocumentColumn("Half", width=0.8, align="right"),
                DocumentColumn("Leave (paid)", width=0.9, align="right"),
                DocumentColumn("Payable", width=0.9, align="right"),
            ),
            rows=tuple(
                (
                    row["employee"],
                    row["present"],
                    row["absent"],
                    row["half"],
                    row["leave_paid"],
                    row["payable_days"],
                )
                for row in shaped
            ),
        )
    ]
    view = DocumentView(
        title=title,
        subtitle=f"The live register · {resolver.book.name}",
        reference=period,
        fields=(
            ("Period", _period_label(period)),
            ("Rows", str(len(shaped))),
        ),
        sections=tuple(sections),
        totals=(
            ("Employees", str(len(shaped))),
            (
                "Payable days",
                str(_money(sum((Decimal(r["payable_days"]) for r in shaped), Decimal("0.00")))),
            ),
        ),
        footnotes=(
            "Payable days = present + half × 0.5 + leave-with-pay (ADR-029) — the same "
            "arithmetic a per-day pay head applies.",
            "The live register is the period's latest import; corrections are new "
            "imports, never edits (Law 2).",
        ),
        filename=f"Attendance {period}",
    )
    return PackReport(title=title, filename=f"Attendance {period}", view=view)


def loans_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The loans & advances position — every employee with a live loan:
    issued, recovered, outstanding (the fold the recovery guard runs) and
    each loan's schedule with its paid-to-date. JSON for the UI, CSV for
    the machine, PDF for the file. ``?employee=`` scopes one person."""
    documents = resolver.prior_documents(META_KEY)
    reversed_ids = {d.reversal_of for d in documents if d.reversal_of is not None}
    employees: list[str] = []
    for d in documents:
        if d.reversal_of is not None or d.voucher_id in reversed_ids:
            continue
        doc = (d.meta or {}).get(META_KEY)
        if not isinstance(doc, dict) or doc.get("mode") != "loan":
            continue
        name = str(doc.get("employee") or "").strip()
        if name and name not in employees:
            employees.append(name)
    requested = str(params.get("employee") or "").strip()
    if requested:
        employees = [requested] if requested in employees else []

    rows: list[dict] = []
    for name in employees:
        state = _fold(documents, name)
        position = loan_position_from_state(state)
        rows.append(
            {
                "employee": name,
                "issued": str(position.issued),
                "recovered": str(position.recovered),
                "outstanding": str(position.outstanding),
                "loans": [
                    {
                        "voucher_no": no,
                        "principal": str(loan["principal"]),
                        "rate": str(loan.get("rate") or "0"),
                        "months": loan.get("months"),
                        "emi": loan.get("emi"),
                        "first_period": loan.get("first_period"),
                        "recovered": str(loan["recovered"]),
                        "outstanding": str(
                            _decimal(loan["principal"], "principal") - loan["recovered"]
                        ),
                        "schedule": loan.get("schedule") or [],
                    }
                    for no, loan in sorted(state.loans.items())
                ],
            }
        )

    fmt = str(params.get("format") or "").strip().lower()
    title = "Loans & advances"
    if fmt == "csv":
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["employee", "issued", "recovered", "outstanding", "loans"])
        for row in rows:
            writer.writerow(
                [
                    row["employee"],
                    row["issued"],
                    row["recovered"],
                    row["outstanding"],
                    str(len(row["loans"])),
                ]
            )
        return PackReport(title=title, filename="Loans and advances", csv=buf.getvalue())
    if fmt == "json":
        return PackReport(
            title=title,
            filename="Loans and advances",
            data={"employees": rows},
        )
    sections = [
        DocumentSection(
            title="Positions",
            columns=(
                DocumentColumn("Employee", width=2.4),
                DocumentColumn("Issued", width=1.0, align="right"),
                DocumentColumn("Recovered", width=1.0, align="right"),
                DocumentColumn("Outstanding", width=1.0, align="right"),
            ),
            rows=tuple(
                (
                    r["employee"],
                    _view_amount(r["issued"]),
                    _view_amount(r["recovered"]),
                    _view_amount(r["outstanding"]),
                )
                for r in rows
            ),
        )
    ]
    for row in rows:
        for loan in row["loans"]:
            sections.append(
                DocumentSection(
                    title=(
                        f"{row['employee']} — loan {loan['voucher_no']} "
                        f"({loan['months']} months @ {loan['rate']}% p.a.)"
                    ),
                    columns=(
                        DocumentColumn("Period", width=1.2),
                        DocumentColumn("Opening", width=1.0, align="right"),
                        DocumentColumn("Interest", width=0.9, align="right"),
                        DocumentColumn("Principal", width=1.0, align="right"),
                        DocumentColumn("EMI", width=0.9, align="right"),
                        DocumentColumn("Closing", width=1.0, align="right"),
                    ),
                    rows=tuple(
                        (
                            str(s.get("period")),
                            _view_amount(s.get("opening")),
                            _view_amount(s.get("interest")),
                            _view_amount(s.get("principal")),
                            _view_amount(s.get("emi")),
                            _view_amount(s.get("closing")),
                        )
                        for s in loan["schedule"]
                    ),
                )
            )
    view = DocumentView(
        title=title,
        subtitle=f"The outstanding fold · {resolver.book.name}",
        reference="loans",
        fields=(
            ("Employees with loans", str(len(rows))),
            (
                "Outstanding (all)",
                str(_money(sum((Decimal(r["outstanding"]) for r in rows), Decimal("0.00")))),
            ),
        ),
        sections=tuple(sections),
        totals=(),
        footnotes=(
            "Outstanding = issued − recovered, the fold the recovery guard runs "
            "(ADR-029) — a run can never cross it.",
            "The EMI schedule is advisory: recovery is a per-run fact; the schedule "
            "prices what is due, the runs decide what is taken.",
            "A direct repayment outside payroll (bank pays the lender) is a plain "
            "voucher this fold does not count — reconcile the advance ledger for "
            "the book truth.",
        ),
        filename="Loans and advances",
    )
    return PackReport(title=title, filename="Loans and advances", view=view)


# ------------------------------------------- ADR-029: the settlement print
def settlement_view(ctx: DocumentContext) -> DocumentView | None:
    """The print layout for ``mode: "settlement"`` documents (ADR-029) —
    the full & final statement: encashment, dues, deductions and
    recoveries in the payslip's visual language, every figure the
    document's own immutable ``computed`` block."""
    doc = ctx.meta.get(META_KEY)
    if not isinstance(doc, dict) or doc.get("mode") != "settlement":
        return None
    computed = doc.get("computed") if isinstance(doc.get("computed"), dict) else {}
    encashment = computed.get("encashment") if isinstance(computed.get("encashment"), dict) else {}

    def rows_of(key: str, kind_label: str) -> list[tuple[str, ...]]:
        return [
            (
                str(item.get("name") or ""),
                _view_amount(item.get("amount")),
                kind_label,
            )
            for item in doc.get(key) or []
            if isinstance(item, dict)
        ]

    sections = []
    enc_rows: list[tuple[str, ...]] = []
    if Decimal(str(encashment.get("amount") or "0")) > 0:
        enc_rows.append(
            (
                f"Leave encashment ({encashment.get('days')} days "
                f"× {_view_amount(encashment.get('per_day'))})",
                _view_amount(encashment.get("amount")),
                "earning",
            )
        )
    if enc_rows:
        sections.append(
            DocumentSection(
                title="Encashment",
                columns=(
                    DocumentColumn("Item", width=3.0),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("", width=0.8),
                ),
                rows=tuple(enc_rows),
            )
        )
    other = [
        *rows_of("earnings", "earning"),
        *rows_of("withheld", "withheld"),
        *rows_of("recoveries", "recovery"),
    ]
    if other:
        sections.append(
            DocumentSection(
                title="Dues, deductions & recoveries",
                columns=(
                    DocumentColumn("Component", width=2.6),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("Kind", width=0.9),
                ),
                rows=tuple(other),
            )
        )

    fields: list[tuple[str, str]] = [
        ("Employee", str(doc.get("employee") or "")),
        ("Period", _period_label(str(computed.get("period") or doc.get("period") or ""))),
        ("Settlement date", ctx.voucher_date.isoformat()),
        ("Voucher", f"{ctx.voucher_type} {ctx.voucher_no}"),
    ]
    if computed.get("paid_direct"):
        fields.append(("Paid", "direct — net credited the bank with the settlement"))
    if computed.get("payable_after") is not None:
        fields.append(("Salary payable after", _view_amount(computed.get("payable_after"))))
    if computed.get("loan_outstanding_after") is not None:
        fields.append(
            ("Loan outstanding after", _view_amount(computed.get("loan_outstanding_after")))
        )

    return DocumentView(
        title="Full & final settlement",
        subtitle=f"The relieving statement · {ctx.book_name}",
        reference=ctx.voucher_no,
        fields=tuple(fields),
        sections=tuple(sections),
        totals=(
            ("Gross dues", _view_amount(computed.get("gross"))),
            ("Deductions (withheld)", _view_amount(computed.get("withheld"))),
            ("Recoveries", _view_amount(computed.get("recovered"))),
            ("Net settlement", _view_amount(computed.get("net"))),
        ),
        footnotes=(
            "Leave encashment is days × rate — the operator's facts (ADR-029); a "
            "leave balance needs an accrual policy this pack refuses to invent.",
            "After this settlement the employee is relieved: runs, loans and "
            "further settlements refuse; payouts stay legal.",
            "Computer-generated from immutable posted entries (Law 2) — reprints are identical.",
        ),
        filename=f"Settlement {doc.get('employee') or ''} {computed.get('period') or ''}".replace(
            "  ", " "
        ),
    )
