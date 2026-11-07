"""The statutory reporting bridge: PF & ESI statements from payroll folds (ADR-014).

The payroll pack's own docstring (ADR-012) named its after-commit seam "the
spot a payslip PDF generator **or a PF filing bridge** subscribes to" — this
pack is that bridge kept: the fourth business pack, again with **zero schema
change**, and the first consumer of plugin API 0.6 pack reports.

What it does: fold one work month's payroll runs — through
``payroll.period_wages``, the same document sequence and the same reversal
netting the payslips replay — into the two Indian statutory statements an
employer files:

- **EPF (Provident Fund)**, ``GET /api/v1/reports/pack/pf?period=YYYY-MM`` —
  the ECR's core wage-and-contribution columns per member: UAN, gross wages,
  the EPF/EPS/EDLI wage bases (the configured wage components, capped at the
  statutory ceiling), the employee share (12% default), the pension share
  (8.33% default), the employer's EPF share (the remainder of the employee
  rate, 3.67% at defaults) and EDLI.
- **ESI (Employee State Insurance)**, ``GET /api/v1/reports/pack/esi?period=YYYY-MM``
  — per insured person: IP number, gross wages, the insurability test
  (monthly gross at/below the threshold, 21,000 default), the employee
  (0.75%) and employer (3.25%) contributions.
- **TDS (Tax Deducted at Source)**, ``GET /api/v1/reports/pack/tds?period=YYYY-MM``
  — per deductee: PAN, gross wages, the TDS the runs actually withheld
  (the ``TDS`` / ``Income Tax`` components by default — a parameter), the
  other withheld components alongside it, and the total withheld. A
  deduction *record*, not a slab computation: the regime, declarations and
  rebates that drive the slabs are the payroll sender's facts, never this
  pack's to invent — the statement reports exactly what was withheld.

What it deliberately is NOT: a portal-certified file generator. Rates,
ceilings, thresholds and the wage-component set are **parameters with
long-standing statutory defaults** — they change by notification, not by
code — and every statement carries the applied rules in its footer so a
filing can be checked before upload. Statutory identifiers (UAN, ESI IP
number) ride on the run documents' employee entries — extra fields persist
exactly as posted (ADR-012) — and print blank with a footnote when absent,
never invented: a fabricated UAN is worse than a blank one.

Both formats come from the same fold: ``?format=csv`` serves the machine
format (plain decimals, one header row), the default renders the PDF through
the ADR-013 layout engine — the same visual language as payslips and
invoices. All money is exact ``Decimal``, quantized once at the edge (Law 3);
reversals net out (Law 2); books are strangers (ADR-009). The ER share is
derived as *quantized(EE rate × base) − EPS share*, so employee + employer
shares always reconstruct the full rate at the applied rounding.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..errors import ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentSection,
    DocumentView,
    PackReport,
    PluginManifest,
    Resolver,
)
from ..reporting.printing import format_amount
from .payroll import EmployeeWages, period_wages

PLUGIN = PluginManifest(name="statutory", version="0.2.0", api_compat="0.6")

PF_KEY = "pf"
ESI_KEY = "esi"
TDS_KEY = "tds"

CENT = Decimal("0.01")
RUPEE = Decimal("1")

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
    """``2026-10`` → ``October 2026`` (already validated by the fold)."""
    raw = period.strip()
    if len(raw) == 7 and raw[4] == "-":
        try:
            return f"{_MONTHS[int(raw[5:7]) - 1]} {raw[:4]}"
        except (ValueError, IndexError):
            pass
    return raw


# ------------------------------------------------------------------ the rules
@dataclass(frozen=True)
class PfRules:
    """EPF wage bases and rates — the long-standing figures as defaults,
    every one of them a parameter (they change by notification, not code)."""

    #: Earning components that constitute the PF wage base (basic + DA
    #: conventionally); matched by exact component name.
    wage_components: tuple[str, ...] = ("Basic Salary", "Basic", "DA", "Dearness Allowance")
    #: The statutory wage cap: EPF/EPS/EDLI bases never exceed it.
    wage_ceiling: Decimal = Decimal("15000.00")
    #: Employee share, of the (capped) EPF base.
    ee_rate: Decimal = Decimal("0.12")
    #: Pension share, of the (capped) EPS base.
    eps_rate: Decimal = Decimal("0.0833")
    #: EDLI charges, of the (capped) EDLI base.
    edli_rate: Decimal = Decimal("0.005")


@dataclass(frozen=True)
class EsiRules:
    """ESI rates and the insurability threshold — parameters with the
    long-standing defaults."""

    #: Monthly gross at/below the threshold is insurable.
    wage_threshold: Decimal = Decimal("21000.00")
    ee_rate: Decimal = Decimal("0.0075")  # employee share, of gross
    er_rate: Decimal = Decimal("0.0325")  # employer share, of gross


@dataclass(frozen=True)
class TdsRules:
    """TDS deduction sources — the withheld components that constitute the
    tax deducted at source. What the runs actually withheld, never a slab
    computation: the regime, declarations and rebates that drive the slabs
    are the payroll sender's facts, not this pack's to invent."""

    #: Withheld components counted as TDS; matched by exact component name.
    components: tuple[str, ...] = ("TDS", "Income Tax", "IT")


# ------------------------------------------------------------ param parsing
def _required_period(params: dict[str, str]) -> str:
    period = str(params.get("period") or "").strip()
    if not period:
        raise ValidationError(
            "statutory: pass ?period=YYYY-MM — the wage month the statement covers"
        )
    return period


def _format(params: dict[str, str]) -> str:
    fmt = str(params.get("format") or "pdf").strip().lower()
    if fmt not in ("pdf", "csv"):
        raise ValidationError(f"statutory: format must be one of pdf, csv, got {fmt!r}")
    return fmt


def _quantizer(params: dict[str, str]) -> Callable[[Decimal], Decimal]:
    """The edge rounding (Law 3): cents by default, whole rupees on request."""
    mode = str(params.get("round") or "cent").strip().lower()
    if mode == "cent":
        return lambda value: value.quantize(CENT, rounding=ROUND_HALF_UP)
    if mode == "rupee":
        return lambda value: value.quantize(RUPEE, rounding=ROUND_HALF_UP)
    raise ValidationError(f"statutory: round must be one of cent, rupee, got {mode!r}")


def _decimal_param(params: dict[str, str], name: str, default: Decimal) -> Decimal:
    raw = str(params.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except ArithmeticError:
        raise ValidationError(f"statutory: {name} must be a number, got {raw!r}") from None
    if not value.is_finite() or value < 0:
        raise ValidationError(f"statutory: {name} must be a non-negative number, got {raw!r}")
    return value


def _wage_components(params: dict[str, str]) -> tuple[str, ...]:
    raw = str(params.get("wage") or "").strip()
    if not raw:
        return PfRules.wage_components
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ValidationError(
            "statutory: wage= lists the earning components that constitute the "
            "PF wage base, comma-separated (e.g. wage=Basic Salary,DA)"
        )
    return names


def _tds_components(params: dict[str, str]) -> tuple[str, ...]:
    raw = str(params.get("component") or "").strip()
    if not raw:
        return TdsRules.components
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise ValidationError(
            "statutory: component= lists the withheld components that constitute "
            "TDS, comma-separated (e.g. component=TDS,Income Tax)"
        )
    return names


def _identifier(extras: dict, *keys: str) -> str:
    """The first present identifier among ``keys``, stripped — blank when the
    run didn't carry one. Never invented."""
    for key in keys:
        value = str(extras.get(key) or "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------------------- the PF fold
@dataclass(frozen=True)
class PfMember:
    """One member's PF row — the ECR's core columns, exact Decimals."""

    uan: str
    name: str
    gross_wages: Decimal
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal
    ee_share: Decimal
    eps_share: Decimal
    er_share: Decimal
    edli_share: Decimal


def pf_members(
    wages: list[EmployeeWages], rules: PfRules, quantize: Callable[[Decimal], Decimal]
) -> list[PfMember]:
    members: list[PfMember] = []
    for w in wages:
        wage_base = sum(
            (
                amount
                for (name, kind), amount in w.components.items()
                if kind == "earning" and name in rules.wage_components
            ),
            Decimal("0.00"),
        )
        base = min(wage_base, rules.wage_ceiling).quantize(CENT, rounding=ROUND_HALF_UP)  # the cap
        ee_share = quantize(base * rules.ee_rate)
        eps_share = quantize(base * rules.eps_rate)
        members.append(
            PfMember(
                uan=_identifier(w.extras, "uan"),
                name=w.employee,
                gross_wages=w.gross,
                epf_wages=base,
                eps_wages=base,
                edli_wages=base,
                ee_share=ee_share,
                eps_share=eps_share,
                er_share=ee_share - eps_share,  # the remainder of the EE rate
                edli_share=quantize(base * rules.edli_rate),
            )
        )
    return members


# -------------------------------------------------------------- the ESI fold
@dataclass(frozen=True)
class EsiMember:
    """One insured person's ESI row."""

    ip_no: str
    name: str
    gross_wages: Decimal
    insurable: bool
    ee_contribution: Decimal
    er_contribution: Decimal


def esi_members(
    wages: list[EmployeeWages], rules: EsiRules, quantize: Callable[[Decimal], Decimal]
) -> list[EsiMember]:
    members: list[EsiMember] = []
    for w in wages:
        insurable = Decimal("0.00") < w.gross <= rules.wage_threshold
        members.append(
            EsiMember(
                ip_no=_identifier(w.extras, "esi_ip", "ip"),
                name=w.employee,
                gross_wages=w.gross,
                insurable=insurable,
                ee_contribution=quantize(w.gross * rules.ee_rate) if insurable else Decimal("0.00"),
                er_contribution=quantize(w.gross * rules.er_rate) if insurable else Decimal("0.00"),
            )
        )
    return members


# -------------------------------------------------------------- the TDS fold
@dataclass(frozen=True)
class TdsDeductee:
    """One deductee's TDS row — what the period's runs actually withheld."""

    pan: str
    name: str
    gross_wages: Decimal
    tds: Decimal
    other_withheld: Decimal
    total_withheld: Decimal


def tds_deductees(
    wages: list[EmployeeWages], rules: TdsRules, quantize: Callable[[Decimal], Decimal]
) -> list[TdsDeductee]:
    deductees: list[TdsDeductee] = []
    for w in wages:
        tds = sum(
            (
                amount
                for (name, kind), amount in w.components.items()
                if kind == "withheld" and name in rules.components
            ),
            Decimal("0.00"),
        )
        other = sum(
            (
                amount
                for (name, kind), amount in w.components.items()
                if kind == "withheld" and name not in rules.components
            ),
            Decimal("0.00"),
        )
        deductees.append(
            TdsDeductee(
                pan=_identifier(w.extras, "pan", "pan_no"),
                name=w.employee,
                gross_wages=w.gross,
                tds=quantize(tds),
                other_withheld=quantize(other),
                total_withheld=quantize(tds + other),
            )
        )
    return deductees


# ------------------------------------------------------------------- CSV
PF_CSV_HEADER = (
    "UAN",
    "Member Name",
    "Gross Wages",
    "EPF Wages",
    "EPS Wages",
    "EDLI Wages",
    "EPF EE Share",
    "EPF EPS Share",
    "EPF ER Share",
    "EDLI Charges",
)

ESI_CSV_HEADER = (
    "IP Number",
    "IP Name",
    "Gross Wages",
    "Insurable",
    "EE Contribution",
    "ER Contribution",
)

TDS_CSV_HEADER = (
    "PAN",
    "Deductee Name",
    "Gross Wages",
    "TDS Deducted",
    "Other Withheld",
    "Total Withheld",
)


def _csv(header: tuple[str, ...], rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def pf_csv(members: list[PfMember]) -> str:
    rows = [
        [
            m.uan,
            m.name,
            str(m.gross_wages),
            str(m.epf_wages),
            str(m.eps_wages),
            str(m.edli_wages),
            str(m.ee_share),
            str(m.eps_share),
            str(m.er_share),
            str(m.edli_share),
        ]
        for m in members
    ]
    return _csv(PF_CSV_HEADER, rows)


def esi_csv(members: list[EsiMember]) -> str:
    rows = [
        [
            m.ip_no,
            m.name,
            str(m.gross_wages),
            "yes" if m.insurable else "no",
            str(m.ee_contribution),
            str(m.er_contribution),
        ]
        for m in members
    ]
    return _csv(ESI_CSV_HEADER, rows)


def tds_csv(deductees: list[TdsDeductee]) -> str:
    rows = [
        [
            m.pan,
            m.name,
            str(m.gross_wages),
            str(m.tds),
            str(m.other_withheld),
            str(m.total_withheld),
        ]
        for m in deductees
    ]
    return _csv(TDS_CSV_HEADER, rows)


# ------------------------------------------------------------------- PDF
def _rate_percent(rate: Decimal) -> str:
    return format_amount(rate * 100)


def pf_view(period: str, members: list[PfMember], rules: PfRules, rounding: str) -> DocumentView:
    rows = [
        (
            m.uan or "—",
            m.name,
            format_amount(m.gross_wages),
            format_amount(m.epf_wages),
            format_amount(m.ee_share),
            format_amount(m.eps_share),
            format_amount(m.er_share),
            format_amount(m.edli_share),
        )
        for m in members
    ]
    total = lambda pick: sum((pick(m) for m in members), Decimal("0.00"))  # noqa: E731

    footnotes = [
        f"Wage base: {', '.join(rules.wage_components)}, capped at "
        f"{format_amount(rules.wage_ceiling)} (override with wage= and ceiling=).",
        f"Shares: EE {_rate_percent(rules.ee_rate)}% and EPS {_rate_percent(rules.eps_rate)}% "
        f"of the capped base; ER is the remainder of the EE rate; "
        f"EDLI {_rate_percent(rules.edli_rate)}%.",
        f"Rounding: {rounding} (override with round=).",
        "UANs ride the run documents' employee entries (uan); a dash means the "
        "run did not carry one — fill it at the portal, never here.",
        "Folded from posted payroll runs — reversals net to zero (Law 2). Verify "
        "against the portal's current template before uploading.",
    ]
    zero_base = [m.name for m in members if m.epf_wages == 0]
    if zero_base:
        footnotes.append(
            f"No matching wage components for: {', '.join(zero_base)} — they report "
            "zero bases; check the wage= parameter or the run's components."
        )
    if not members:
        footnotes.append("No payroll runs posted for this period — a nil statement.")

    return DocumentView(
        title="EPF statement",
        subtitle=f"Provident Fund — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("Members", str(len(members))),
            ("Wage ceiling", format_amount(rules.wage_ceiling)),
            ("Rounding", rounding),
        ),
        sections=(
            DocumentSection(
                title="Members",
                columns=(
                    DocumentColumn("UAN", width=1.15),
                    DocumentColumn("Member", width=1.55),
                    DocumentColumn("Gross", width=0.85, align="right"),
                    DocumentColumn("EPF wages", width=0.85, align="right"),
                    DocumentColumn("EE share", width=0.85, align="right"),
                    DocumentColumn("EPS share", width=0.85, align="right"),
                    DocumentColumn("ER share", width=0.85, align="right"),
                    DocumentColumn("EDLI", width=0.75, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Total gross wages", format_amount(total(lambda m: m.gross_wages))),
            ("Total EPF wages", format_amount(total(lambda m: m.epf_wages))),
            ("Total EE share", format_amount(total(lambda m: m.ee_share))),
            ("Total EPS share", format_amount(total(lambda m: m.eps_share))),
            ("Total ER share", format_amount(total(lambda m: m.er_share))),
            ("Total EDLI charges", format_amount(total(lambda m: m.edli_share))),
            (
                "Total contributions",
                format_amount(
                    total(lambda m: m.ee_share + m.eps_share + m.er_share + m.edli_share)
                ),
            ),
        ),
        footnotes=tuple(footnotes),
        filename=f"PF statement {period}",
    )


def esi_view(period: str, members: list[EsiMember], rules: EsiRules, rounding: str) -> DocumentView:
    rows = [
        (
            m.ip_no or "—",
            m.name,
            format_amount(m.gross_wages),
            "yes" if m.insurable else "no",
            format_amount(m.ee_contribution),
            format_amount(m.er_contribution),
        )
        for m in members
    ]
    total = lambda pick: sum((pick(m) for m in members), Decimal("0.00"))  # noqa: E731

    footnotes = [
        f"Insurable: monthly gross at or below {format_amount(rules.wage_threshold)} "
        "(override with threshold=).",
        f"Shares: EE {_rate_percent(rules.ee_rate)}% and ER {_rate_percent(rules.er_rate)}% "
        "of gross wages, insurable members only.",
        f"Rounding: {rounding} (override with round=).",
        "IP numbers ride the run documents' employee entries (esi_ip or ip); a dash "
        "means the run did not carry one — fill it at the portal, never here.",
        "Folded from posted payroll runs — reversals net to zero (Law 2). Verify "
        "against the portal's current template before uploading.",
    ]
    if not any(m.insurable for m in members):
        footnotes.append("No insurable members this period — a nil statement.")
    if not members:
        footnotes.append("No payroll runs posted for this period — a nil statement.")

    return DocumentView(
        title="ESI statement",
        subtitle=f"Employee State Insurance — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("Insured persons", str(sum(1 for m in members if m.insurable))),
            ("Wage threshold", format_amount(rules.wage_threshold)),
            ("Rounding", rounding),
        ),
        sections=(
            DocumentSection(
                title="Insured persons",
                columns=(
                    DocumentColumn("IP number", width=1.2),
                    DocumentColumn("Name", width=1.9),
                    DocumentColumn("Gross wages", width=1.0, align="right"),
                    DocumentColumn("Insurable", width=0.7),
                    DocumentColumn("EE share", width=0.9, align="right"),
                    DocumentColumn("ER share", width=0.9, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Total gross wages", format_amount(total(lambda m: m.gross_wages))),
            ("Total EE contributions", format_amount(total(lambda m: m.ee_contribution))),
            ("Total ER contributions", format_amount(total(lambda m: m.er_contribution))),
        ),
        footnotes=tuple(footnotes),
        filename=f"ESI statement {period}",
    )


def tds_view(
    period: str, deductees: list[TdsDeductee], rules: TdsRules, rounding: str
) -> DocumentView:
    rows = [
        (
            m.pan or "—",
            m.name,
            format_amount(m.gross_wages),
            format_amount(m.tds),
            format_amount(m.other_withheld),
            format_amount(m.total_withheld),
        )
        for m in deductees
    ]
    total = lambda pick: sum((pick(m) for m in deductees), Decimal("0.00"))  # noqa: E731

    footnotes = [
        f"TDS components: {', '.join(rules.components)} (override with component=).",
        f"Rounding: {rounding} (override with round=).",
        "PANs ride the run documents' employee entries (pan); a dash means the "
        "run did not carry one — fill it at the portal, never here.",
        "A deduction record, not a slab computation: the statement reports what "
        "the runs withheld — the regime, declarations and rebates stay the "
        "payroll sender's facts.",
        "Folded from posted payroll runs — reversals net to zero (Law 2). Verify "
        "against the portal's current template before uploading.",
    ]
    if deductees and not any(m.tds for m in deductees):
        footnotes.append("No TDS component withheld this period — a nil statement.")
    if not deductees:
        footnotes.append("No payroll runs posted for this period — a nil statement.")

    return DocumentView(
        title="TDS statement",
        subtitle=f"Tax Deducted at Source — {_period_label(period)}",
        fields=(
            ("Period", _period_label(period)),
            ("Deductees", str(len(deductees))),
            ("TDS components", ", ".join(rules.components)),
            ("Rounding", rounding),
        ),
        sections=(
            DocumentSection(
                title="Deductees",
                columns=(
                    DocumentColumn("PAN", width=1.1),
                    DocumentColumn("Deductee", width=1.6),
                    DocumentColumn("Gross", width=0.95, align="right"),
                    DocumentColumn("TDS", width=0.85, align="right"),
                    DocumentColumn("Other", width=0.85, align="right"),
                    DocumentColumn("Total withheld", width=1.0, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Total gross wages", format_amount(total(lambda m: m.gross_wages))),
            ("Total TDS deducted", format_amount(total(lambda m: m.tds))),
            ("Total other withheld", format_amount(total(lambda m: m.other_withheld))),
            ("Total withheld", format_amount(total(lambda m: m.total_withheld))),
        ),
        footnotes=tuple(footnotes),
        filename=f"TDS statement {period}",
    )


# ------------------------------------------------------------- the factories
def pf_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The EPF statement of one wage month, as PDF (default) or CSV."""
    period = _required_period(params)
    wages = period_wages(resolver, period)  # validates the period format (422)
    rules = PfRules(
        wage_components=_wage_components(params),
        wage_ceiling=_decimal_param(params, "ceiling", PfRules.wage_ceiling),
    )
    quantize = _quantizer(params)
    members = pf_members(wages, rules, quantize)
    if _format(params) == "csv":
        return PackReport(
            title=f"EPF statement {period}", filename=f"PF statement {period}", csv=pf_csv(members)
        )
    return PackReport(
        title=f"EPF statement {period}",
        filename=f"PF statement {period}",
        view=pf_view(period, members, rules, str(params.get("round") or "cent").strip().lower()),
    )


def esi_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The ESI statement of one wage month, as PDF (default) or CSV."""
    period = _required_period(params)
    wages = period_wages(resolver, period)  # validates the period format (422)
    rules = EsiRules(wage_threshold=_decimal_param(params, "threshold", EsiRules.wage_threshold))
    quantize = _quantizer(params)
    members = esi_members(wages, rules, quantize)
    if _format(params) == "csv":
        return PackReport(
            title=f"ESI statement {period}",
            filename=f"ESI statement {period}",
            csv=esi_csv(members),
        )
    return PackReport(
        title=f"ESI statement {period}",
        filename=f"ESI statement {period}",
        view=esi_view(period, members, rules, str(params.get("round") or "cent").strip().lower()),
    )


def tds_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """The TDS statement of one wage month, as PDF (default) or CSV — the
    second pack report: what the runs actually withheld, per deductee."""
    period = _required_period(params)
    wages = period_wages(resolver, period)  # validates the period format (422)
    rules = TdsRules(components=_tds_components(params))
    quantize = _quantizer(params)
    deductees = tds_deductees(wages, rules, quantize)
    if _format(params) == "csv":
        return PackReport(
            title=f"TDS statement {period}",
            filename=f"TDS statement {period}",
            csv=tds_csv(deductees),
        )
    return PackReport(
        title=f"TDS statement {period}",
        filename=f"TDS statement {period}",
        view=tds_view(period, deductees, rules, str(params.get("round") or "cent").strip().lower()),
    )


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_report(PF_KEY, pf_report)
    registry.register_report(ESI_KEY, esi_report)
    registry.register_report(TDS_KEY, tds_report)


__all__ = [
    "ESI_KEY",
    "ESI_CSV_HEADER",
    "PF_CSV_HEADER",
    "PF_KEY",
    "PLUGIN",
    "TDS_KEY",
    "TDS_CSV_HEADER",
    "esi_csv",
    "esi_members",
    "esi_report",
    "esi_view",
    "pf_csv",
    "pf_members",
    "pf_report",
    "pf_view",
    "register",
    "tds_csv",
    "tds_deductees",
    "tds_report",
    "tds_view",
]
