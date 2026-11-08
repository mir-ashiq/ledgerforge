"""The banking pack (ADR-026, roadmap Phase 5): the meta layer over the
banking kernel — cheque declarations on vouchers, the reconciliation and
cheque-register pack reports, the cheque-aware payment print view, and the
gateway webhook that delivers statements in-process.

The division of labour (ADR-026 §6): the kernel owns the tables, the
parsers, the import service, the matchmaker and the API routes; the pack
owns everything that interprets ``meta`` — the kernel stays meta-blind
(ADR-010) while cheque declarations, being documents that ride vouchers,
are exactly what the meta store is for (the ADR-023 precedent, applied to
banking).

One meta key, ``meta.banking``, one document kind:

    {"cheque": {"no": "448812", "bank_ledger": "HDFC Current",
                 "issued_on": "2026-11-01",      # default: voucher date
                 "matures_on": "2026-12-01"}}     # a PDC's maturity

The validator enforces: the ledger exists in the book, the voucher
actually moves it (a cheque rides a payment/receipt through its own bank
account — TallyPrime's rule), a PDC matures on or after the issue date,
and — under the resolver's advisory lock — that no other live voucher of
the same ledger declares the same number (a reversal pair kills its
cheque with it, the bills fold's skip-undone rule).

The cheque lifecycle is a **fold, not a status column** (the order-position
philosophy, ADR-024): *issued* when the voucher posts, *presented* when an
imported statement line of that ledger references the number, *cleared*
when such a line carries a live match — both sides agree. PDC tracking is
the maturity date against the report's as-of.

Registered surfaces:

- ``banking-reconciliation`` — the book-vs-bank statement with the exact
  proof, ONE truth: the pack resolves the ledger, demands the accountant's
  anchor (the closing balance on the statement) and delegates to the
  kernel fold through the resolver's documented 0.11 handles — the route
  and the PDF are two renderings of the same numbers, never two maths;
  the tests prove route ≡ pack report;
- ``banking-cheques`` — the register: every live declaration with its
  derived lifecycle and PDC maturity, filterable by ledger and status;
- the ``banking`` document view — a cheque-carrying payment/receipt prints
  with its cheque number, date and maturity in the header fields;
- the ``bank-statement-gateway`` webhook — the generic gateway JSON shape
  any connected-banking bridge can POST (per-bank adapters translate their
  bank's payload; core never learns a bank's name — ADR-026 D3).

All money is exact ``Decimal`` (Law 3); amounts cross the wire as strings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ..banking import import_statement
from ..banking import reconciliation as kernel_reconciliation
from ..errors import NotFoundError, ValidationError
from ..plugins import (
    DocumentColumn,
    DocumentSection,
    DocumentView,
    PackReport,
    PluginManifest,
    Resolver,
)
from ..reporting.printing import format_amount

logger = logging.getLogger("ledgerforge.plugins.banking")

PLUGIN = PluginManifest(name="banking", version="0.1.0", api_compat="0.11")

META_KEY = "banking"

_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def register(registry) -> None:  # noqa: ANN001 — registry is the stable API object
    registry.register_validator(validate_banking_meta)
    registry.register_report("banking-reconciliation", reconciliation_report)
    registry.register_report("banking-cheques", cheques_report)
    registry.register_document_view(META_KEY, banking_document_view)
    registry.register_webhook("bank-statement-gateway", gateway_hook)


# ---------------------------------------------------------------- validator
def validate_banking_meta(draft, resolver: Resolver) -> None:  # noqa: ANN001
    """The cheque-declaration law, enforced after every transformer (the
    engine's ordering): structure, the ledger, the movement, the maturity,
    and the per-ledger number uniqueness — under the advisory lock, the
    posting engine's own serialization discipline."""
    raw = draft.meta.get(META_KEY)
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValidationError("banking: meta.banking must be an object")
    unknown = set(raw) - {"cheque"}
    if unknown:
        raise ValidationError(
            f"banking: unknown meta.banking keys ({', '.join(sorted(unknown))}) — "
            "only cheque declarations ride this key"
        )
    cheque = raw.get("cheque")
    if not isinstance(cheque, dict):
        raise ValidationError("banking: meta.banking.cheque must be an object")
    unknown_c = set(cheque) - {"no", "bank_ledger", "issued_on", "matures_on"}
    if unknown_c:
        raise ValidationError(
            f"banking: unknown cheque keys ({', '.join(sorted(unknown_c))}) — "
            "no, bank_ledger, issued_on, matures_on"
        )

    no = str(cheque.get("no") or "").strip()
    if not no or len(no) > 34:
        raise ValidationError("banking: cheque.no is required (1–34 characters)")
    bank_name = str(cheque.get("bank_ledger") or "").strip()
    if not bank_name:
        raise ValidationError(
            "banking: cheque.bank_ledger is required — the bank account this cheque draws on"
        )
    bank = resolver.ledger(name=bank_name)
    if bank is None:
        raise ValidationError(
            f"banking: cheque.bank_ledger {bank_name!r} does not exist in this book"
        )

    # a cheque rides a voucher that moves its own bank account
    moves_bank = any(
        line.ledger_name == bank_name or line.ledger_id == bank.id for line in draft.lines
    )
    if not moves_bank:
        raise ValidationError(
            f"banking: the voucher does not move bank ledger {bank_name!r} — "
            "a cheque declaration rides a payment/receipt through its own account"
        )

    issued_on = cheque.get("issued_on")
    if issued_on is not None:
        try:
            date.fromisoformat(str(issued_on))
        except ValueError:
            raise ValidationError("banking: cheque.issued_on must be YYYY-MM-DD") from None
    matures_on = cheque.get("matures_on")
    if matures_on is not None:
        try:
            matures_on = date.fromisoformat(str(matures_on))
        except ValueError:
            raise ValidationError("banking: cheque.matures_on must be YYYY-MM-DD") from None
        if matures_on < draft.voucher_date:
            raise ValidationError(
                "banking: cheque.matures_on precedes the voucher date — a "
                "post-dated cheque matures on or after its issue"
            )

    # per-ledger number uniqueness among LIVE declarations — the advisory
    # lock serialises concurrent posts of the same number
    resolver.lock(f"banking:cheque:{bank.id}:{no}")
    for key, live in _live_cheques(resolver).items():
        if key == (bank_name, no):
            raise ValidationError(
                f"banking: cheque {no!r} of {bank_name!r} is already declared on "
                f"voucher {live} — a number is one cheque"
            )


@dataclass(frozen=True)
class ChequeDeclaration:
    """One cheque as declared on a posted voucher (the fold's atom)."""

    voucher_id: int
    voucher_no: str
    voucher_date: date
    no: str
    bank_ledger: str
    issued_on: date
    matures_on: date | None
    amount: Decimal | None = None  # the bank line's amount, resolved in the fold


def _iter_declarations(resolver: Resolver) -> list[tuple[object, dict]]:
    """(PriorDocument, cheque dict) for every live declaration — a reversal
    pair kills its cheque with it (the bills fold's skip-undone rule:
    undoing a payment un-issues its cheque)."""
    docs = resolver.prior_documents(META_KEY)
    reversed_ids = {d.reversal_of for d in docs if d.reversal_of is not None}
    out = []
    for doc in sorted(docs, key=lambda d: (d.voucher_date, d.voucher_id)):
        if doc.reversal_of is not None or doc.voucher_id in reversed_ids:
            continue  # the reversal, or the undone original
        banking = (doc.meta or {}).get("banking")
        if not isinstance(banking, dict):
            continue
        cheque = banking.get("cheque")
        if isinstance(cheque, dict) and cheque.get("no") and cheque.get("bank_ledger"):
            out.append((doc, cheque))
    return out


def _live_cheques(resolver: Resolver) -> dict[tuple[str, str], str]:
    """(bank_ledger, no) → voucher_no for every live declaration."""
    return {
        (str(c["bank_ledger"]), str(c["no"])): d.voucher_no for d, c in _iter_declarations(resolver)
    }


def _declarations(resolver: Resolver) -> list[ChequeDeclaration]:
    """Every live cheque declaration, oldest first."""
    out: list[ChequeDeclaration] = []
    for doc, cheque in _iter_declarations(resolver):
        try:
            issued_on = (
                date.fromisoformat(str(cheque["issued_on"]))
                if cheque.get("issued_on")
                else doc.voucher_date
            )
        except ValueError:
            issued_on = doc.voucher_date
        try:
            matures_on = (
                date.fromisoformat(str(cheque["matures_on"])) if cheque.get("matures_on") else None
            )
        except ValueError:
            matures_on = None
        out.append(
            ChequeDeclaration(
                voucher_id=doc.voucher_id,
                voucher_no=doc.voucher_no,
                voucher_date=doc.voucher_date,
                no=str(cheque["no"]),
                bank_ledger=str(cheque["bank_ledger"]),
                issued_on=issued_on,
                matures_on=matures_on,
            )
        )
    return out


def _line_mentions_cheque(line, cheque_no: str) -> bool:  # noqa: ANN001 — BankLineRow
    """Does an imported statement line reference this cheque number?
    Precise by construction: the parsed cheque field's digits equal the
    number, or one of the text's whole digit-runs equals it ('CHQ 448812'
    and 'UPI/448812/…' both match; a longer run like 4488123 does NOT —
    over-matching a substring is how wrong cheques clear)."""
    needle = re.sub(r"\D", "", cheque_no)
    if not needle:
        return False
    if line.cheque_no and re.sub(r"\D", "", str(line.cheque_no)) == needle:
        return True
    for text in (line.reference, line.narrative):
        if text and any(run == needle for run in re.findall(r"\d+", str(text))):
            return True
    return False


def _cheque_rows(resolver: Resolver, as_of: date) -> list[dict]:
    """The register fold: declarations × statement lines × matches —
    issued → presented → cleared, PDC by maturity. A pure function of the
    sanctioned reads (``prior_documents`` + ``ledger`` + ``bank_lines`` +
    ``ledger_entries``)."""
    lines_by_ledger: dict[str, list] = {}
    entries_by_ledger: dict[str, dict[int, Decimal]] = {}
    rows: list[dict] = []
    for decl in _declarations(resolver):
        if decl.bank_ledger not in lines_by_ledger:
            ledger = resolver.ledger(name=decl.bank_ledger)
            if ledger is None:
                lines_by_ledger[decl.bank_ledger] = []
                entries_by_ledger[decl.bank_ledger] = {}
            else:
                lines_by_ledger[decl.bank_ledger] = [
                    ln for ln in resolver.bank_lines(ledger.id) if ln.value_date <= as_of
                ]
                bank_amounts: dict[int, Decimal] = {}
                for e in resolver.ledger_entries(ledger.id):
                    bank_amounts.setdefault(e.voucher_id, e.amount)
                entries_by_ledger[decl.bank_ledger] = bank_amounts
        lines = lines_by_ledger[decl.bank_ledger]
        entries = entries_by_ledger[decl.bank_ledger]

        presented = None
        cleared_by = None
        for line in lines:
            if _line_mentions_cheque(line, decl.no):
                if presented is None:
                    presented = line.value_date.isoformat()
                if line.match is not None and line.match.live:
                    cleared_by = line.match.voucher_no
                    break
        status = "cleared" if cleared_by else ("presented" if presented else "issued")
        amount = entries.get(decl.voucher_id)
        rows.append(
            {
                "bank_ledger": decl.bank_ledger,
                "no": decl.no,
                "voucher_no": decl.voucher_no,
                "voucher_date": decl.voucher_date.isoformat(),
                "issued_on": decl.issued_on.isoformat(),
                "matures_on": decl.matures_on.isoformat() if decl.matures_on else None,
                "amount": str(_money(amount)) if amount is not None else None,
                "status": status,
                "presented_on": presented,
                "cleared_by": cleared_by,
                "pdc": bool(decl.matures_on and decl.matures_on > decl.voucher_date),
                "matured_as_of": bool(decl.matures_on and decl.matures_on <= as_of),
            }
        )
    return rows


# ------------------------------------------------------------------ reports
def _require_date(params: dict[str, str], key: str) -> date:
    raw = params.get(key)
    if not raw:
        raise ValidationError(f"banking: the {key} parameter is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"banking: {key} must be YYYY-MM-DD") from None


def reconciliation_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """``banking-reconciliation`` — the book-vs-bank statement with the
    proof. ONE truth: the pack resolves the ledger, demands the
    accountant's anchor (the closing balance on the statement — a
    printable statement names its anchor) and delegates to the kernel fold
    through the resolver's documented 0.11 handles. The route and the PDF
    are two renderings of the same numbers, never two maths; the tests
    prove route ≡ pack report."""
    ledger_name = (params.get("ledger") or "").strip()
    if not ledger_name:
        raise ValidationError("banking: the ledger parameter is required (the bank account's name)")
    as_of = _require_date(params, "as_of")
    raw_balance = (params.get("statement_balance") or "").strip()
    if not raw_balance:
        raise ValidationError(
            "banking: statement_balance is required — the closing balance on "
            "the statement (the printable statement names its anchor)"
        )

    ledger = resolver.ledger(name=ledger_name)
    if ledger is None:
        raise NotFoundError(f"banking: ledger {ledger_name!r} does not exist in this book")
    data = kernel_reconciliation(
        resolver.session,
        book=resolver.book,
        ledger=ledger,
        as_of=as_of,
        statement_balance=raw_balance,
    )
    return PackReport(
        title="Bank Reconciliation Statement",
        filename=f"bank-reconciliation {ledger_name} {as_of.isoformat()}",
        view=_reconciliation_view(data),
        data=data,
        csv=_reconciliation_csv(data),
    )


def _reconciliation_view(data: dict) -> DocumentView:
    rows = tuple(
        (
            r["label"],
            r["date"],
            r["reference"] or "",
            (r["narration"] or "")[:40],
            r["effect"],
        )
        for r in data["rows"]
    )
    return DocumentView(
        title="Bank Reconciliation Statement",
        subtitle=f"{data['ledger_name']} — as of {data['as_of']}",
        reference=data["as_of"],
        fields=(
            ("Bank account", data["ledger_name"]),
            ("As of", data["as_of"]),
            ("Balance as per books", format_amount(Decimal(data["book_balance"]))),
            ("Balance as per bank statement", format_amount(Decimal(data["statement_balance"]))),
        ),
        sections=(
            DocumentSection(
                title="Unreconciled items",
                columns=(
                    DocumentColumn("Particulars", width=2.4),
                    DocumentColumn("Date", width=0.8),
                    DocumentColumn("Reference", width=1.0),
                    DocumentColumn("Narration", width=1.8),
                    DocumentColumn("Effect", width=1.0, align="right"),
                ),
                rows=rows,
            ),
        ),
        totals=(
            ("Unmatched statement net", format_amount(Decimal(data["unmatched_statement_total"]))),
            ("Unmatched books net", format_amount(Decimal(data["unmatched_book_total"]))),
            ("Pair differences", format_amount(Decimal(data["pair_difference_total"]))),
            ("Reconciled balance", format_amount(Decimal(data["reconciled_balance"]))),
            (
                "Proof (book − statement = net effects)",
                format_amount(Decimal(data["proof"]["lhs"])),
            ),
        ),
        footnotes=(
            "Effects are Cr-positive. Book − statement = unmatched-book − "
            "unmatched-statement + pair differences, exactly (ADR-026).",
        ),
    )


def _reconciliation_csv(data: dict) -> str:
    lines_out = [
        f"Bank Reconciliation Statement,{data['ledger_name']},as of,{data['as_of']}",
        f"book_balance,{data['book_balance']}",
        f"statement_balance,{data['statement_balance']}",
        f"reconciled_balance,{data['reconciled_balance']}",
        "kind,label,date,amount,effect,reference,narration",
    ]
    for r in data["rows"]:
        narration = (r["narration"] or "").replace('"', "'")
        lines_out.append(
            f'{r["kind"]},"{r["label"]}",{r["date"]},{r["amount"]},'
            f'{r["effect"]},"{r["reference"] or ""}","{narration}"'
        )
    lines_out.append(f"proof,{data['proof']['lhs']},{data['proof']['rhs']}")
    return "\n".join(lines_out) + "\n"


def cheques_report(resolver: Resolver, params: dict[str, str]) -> PackReport:
    """``banking-cheques`` — the register: every live declaration with its
    derived lifecycle (issued → presented → cleared), PDC maturity against
    the as-of, filterable by ledger and status."""
    as_of = _require_date(params, "as_of")
    ledger_filter = (params.get("ledger") or "").strip()
    status_filter = (params.get("status") or "").strip()

    rows = _cheque_rows(resolver, as_of)
    if ledger_filter:
        rows = [r for r in rows if r["bank_ledger"] == ledger_filter]
    if status_filter:
        if status_filter not in ("issued", "presented", "cleared", "pdc"):
            raise ValidationError(
                "banking: status filter must be issued, presented, cleared or pdc"
            )
        rows = [
            r
            for r in rows
            if (r["status"] == status_filter if status_filter != "pdc" else r["pdc"])
        ]

    by_status = {"issued": 0, "presented": 0, "cleared": 0}
    for r in rows:
        by_status[r["status"]] += 1
    data = {
        "as_of": as_of.isoformat(),
        "ledger": ledger_filter or None,
        "status": status_filter or None,
        "counts": by_status,
        "rows": rows,
    }
    view = DocumentView(
        title="Cheque Register",
        subtitle=(f"as of {as_of.isoformat()}" + (f" — {ledger_filter}" if ledger_filter else "")),
        reference=as_of.isoformat(),
        fields=(
            ("As of", as_of.isoformat()),
            ("Issued", str(by_status["issued"])),
            ("Presented", str(by_status["presented"])),
            ("Cleared", str(by_status["cleared"])),
        ),
        sections=(
            DocumentSection(
                title="Cheques",
                columns=(
                    DocumentColumn("Bank", width=1.3),
                    DocumentColumn("Cheque No", width=1.0),
                    DocumentColumn("Voucher", width=1.0),
                    DocumentColumn("Issued", width=0.8),
                    DocumentColumn("Matures", width=0.8),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("Status", width=0.9),
                    DocumentColumn("Presented", width=0.8),
                    DocumentColumn("Cleared by", width=1.0),
                ),
                rows=tuple(
                    (
                        r["bank_ledger"],
                        r["no"],
                        r["voucher_no"],
                        r["issued_on"],
                        r["matures_on"] or "—",
                        r["amount"] or "—",
                        r["status"] + (" (PDC)" if r["pdc"] else ""),
                        r["presented_on"] or "—",
                        r["cleared_by"] or "—",
                    )
                    for r in rows
                ),
            ),
        ),
        footnotes=(
            "issued → presented (an imported statement line references the "
            "number) → cleared (that line is matched — both sides agree). "
            "PDC: matures after its issue date; maturity tracked against the "
            "as-of (ADR-026).",
        ),
        filename=f"cheque-register {as_of.isoformat()}",
    )
    csv_lines = [
        f"Cheque Register,as of,{as_of.isoformat()}",
        "bank_ledger,cheque_no,voucher,voucher_date,issued_on,matures_on,amount,"
        "status,presented_on,cleared_by,pdc",
    ]
    for r in rows:
        csv_lines.append(
            f'"{r["bank_ledger"]}",{r["no"]},{r["voucher_no"]},{r["voucher_date"]},'
            f"{r['issued_on']},{r['matures_on'] or ''},{r['amount'] or ''},"
            f"{r['status']},{r['presented_on'] or ''},{r['cleared_by'] or ''},"
            f"{'yes' if r['pdc'] else 'no'}"
        )
    return PackReport(
        title="Cheque Register",
        filename=f"cheque-register {as_of.isoformat()}",
        view=view,
        data=data,
        csv="\n".join(csv_lines) + "\n",
    )


# ------------------------------------------------------------ document view
def banking_document_view(ctx) -> DocumentView | None:  # noqa: ANN001 — DocumentContext
    """Claim the print layout of vouchers carrying ``meta.banking`` — a
    cheque-carrying payment/receipt prints with its cheque number, date
    and maturity in the header fields (the fallback voucher slip cannot
    know them; this pack can)."""
    cheque = (ctx.meta or {}).get("cheque")
    if not isinstance(cheque, dict) or not cheque.get("no"):
        return None  # not a cheque voucher — the fallback slip takes over
    fields = (
        ("Bank account", str(cheque.get("bank_ledger") or "")),
        ("Cheque no", str(cheque.get("no") or "")),
        ("Cheque date", str(cheque.get("issued_on") or ctx.voucher_date.isoformat())),
    )
    matures = cheque.get("matures_on")
    if matures:
        fields += (("Matures (PDC)", str(matures)),)
    rows = tuple(
        (
            str(e.line_no),
            e.ledger_name,
            "Dr" if e.dc == "D" else "Cr",
            format_amount(e.amount),
            e.narration or "",
        )
        for e in ctx.entries
    )
    total_dr = sum((e.amount for e in ctx.entries if e.dc == "D"), Decimal(0))
    total_cr = sum((e.amount for e in ctx.entries if e.dc == "C"), Decimal(0))
    return DocumentView(
        title="Payment voucher",
        subtitle=ctx.book_name,
        reference=ctx.voucher_no,
        fields=fields,
        sections=(
            DocumentSection(
                title="Entries",
                columns=(
                    DocumentColumn("#", width=0.3),
                    DocumentColumn("Ledger", width=2.2),
                    DocumentColumn("Dr/Cr", width=0.6),
                    DocumentColumn("Amount", width=1.0, align="right"),
                    DocumentColumn("Narration", width=1.9),
                ),
                rows=rows,
            ),
        ),
        totals=(
            ("Total Dr", format_amount(total_dr)),
            ("Total Cr", format_amount(total_cr)),
        ),
        footnotes=(ctx.narration,) if ctx.narration else (),
        filename=f"payment {ctx.voucher_no}",
    )


# ---------------------------------------------------------------- the hook
def gateway_hook(resolver: Resolver, payload: dict) -> dict:
    """``bank-statement-gateway`` — the generic gateway JSON shape any
    connected-banking bridge can POST: the bank ledger (name or id), the
    format (csv/mt940), the statement text, optional balances, a source
    label and an optional dry_run. Per-bank adapters translate their
    bank's payload into this shape (or call the kernel import directly) —
    the kernel never learns a bank's name (ADR-026 D3)."""
    if not isinstance(payload, dict):
        raise ValidationError("banking gateway: the payload must be a JSON object")
    ledger_ref = payload.get("ledger") or payload.get("bank_ledger")
    if ledger_ref is None:
        raise ValidationError(
            "banking gateway: the payload needs 'ledger' (the bank account's name or id)"
        )
    ledger = (
        resolver.ledger(id=int(ledger_ref))
        if isinstance(ledger_ref, int) or (isinstance(ledger_ref, str) and ledger_ref.isdigit())
        else resolver.ledger(name=str(ledger_ref))
    )
    if ledger is None:
        raise NotFoundError(f"banking gateway: ledger {ledger_ref!r} does not exist in this book")
    fmt = str(payload.get("format") or "").strip().lower()
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("banking gateway: the payload needs 'text' (the statement)")
    receipt = import_statement(
        resolver.session,
        book=resolver.book,
        ledger=ledger,
        fmt=fmt,
        text=text,
        source=str(payload.get("source") or "gateway"),
        opening_balance=payload.get("opening_balance"),
        closing_balance=payload.get("closing_balance"),
        dry_run=bool(payload.get("dry_run", False)),
    )
    _record_gateway(receipt)
    return receipt


# -------------------------------------------------- observability for tests
#: Every import the gateway hook performed while this pack is loaded (the
#: test seams assert on it; a real bridge subscribes to the kernel's
#: ``bank.statement.imported`` event instead of reading this).
GATEWAY_IMPORTS: list[dict] = []


def _record_gateway(receipt: dict) -> None:
    GATEWAY_IMPORTS.append(receipt)
