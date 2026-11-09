"""The banking kernel (ADR-026, roadmap Phase 5): statement import, the
matching engine, and the reconciliation fold.

Three disciplines, one module:

- **Import is validated then idempotent.** CSV and MT940 parse into exact
  ``ParsedLine``s; one bad row refuses the batch (``dry_run`` is the
  preview). Every line carries a content fingerprint —
  ``(date, direction, amount, reference, narrative, occurrence)`` — whose
  UNIQUE constraint makes re-importing the same statement a database-level
  no-op while two genuinely identical transactions (occurrence 0, 1, …)
  both survive. Occurrences are counted **globally per ledger**, so a later
  statement that truly repeats a transaction lands beside the first, never
  eaten by the earlier import.
- **Suggestions never commit.** The matching engine is a pure function of
  the current state: amount-exact candidates, a date window, and
  reference-fuzzy evidence — computed, ranked, returned, written nowhere.
  Only the explicit match routes write, and the pack reports re-read the
  same folds the UIs see.
- **The reconciliation statement is a proof.** As-of a date, with exact
  Decimals:

      book_balance − statement_balance
          = Σ unmatched-book effects − Σ unmatched-statement effects
            + Σ matched-pair differences

  Each side independently computable; the fold asserts the identity and
  the tests assert it against sums computed by other means (the
  trial-balance discipline).

Direction convention (worth stating once, here — v1.0's correction of the
v0.23 passbook mirror): a statement ``C`` line (money into the bank) is
what the books would *debit* — the bank ledger's Dr, an asset growing;
a ``D`` line is its Cr. A matched pair is therefore side-opposite by
nature (a deposit line pairs a Dr-bank entry), and the proof's pair
difference is the pair's asset-side difference: the entry's effect minus
the line's effect, zero when the two sides price the money identically.
All reconciliation figures are asset-positive: the bank balance reads
like the statement, Dr-increases-the-asset.

The kernel stays bank-blind (ADR-026 D3): nothing here knows a bank's
name, URL or payload — per-bank adapters are drop-in plugins that call
:func:`import_statement` in-process or POST the statements route.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .errors import ConflictError, InvariantViolation, NotFoundError, ValidationError
from .models import (
    BankLineMatch,
    BankStatementBatch,
    BankStatementLine,
    Book,
    Group,
    Ledger,
    Voucher,
    VoucherEntry,
)

logger = logging.getLogger("ledgerforge.banking")

#: The seeded groups whose ledgers are bank accounts (TallyPrime's rule —
#: BRS is for bank ledgers; the OD account reconciles like any other).
BANK_GROUPS = ("Bank Accounts", "Bank OD A/c")

#: The default matching date window (days either side): recent first,
#: never far-fetched — the accountant widens it when they must.
DEFAULT_WINDOW_DAYS = 7

_CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------- the parsers
@dataclass
class ParsedLine:
    """One statement transaction as parsed — pre-validation facts. Mutable
    because the MT940 narrative accumulates across continuation lines
    before the line is final."""

    value_date: date
    dc: str  # 'D' = money out per the bank (the books would post Dr)
    amount: Decimal
    reference: str | None = None
    narrative: str | None = None
    cheque_no: str | None = None


@dataclass(frozen=True)
class Reject:
    """One unparsable row — line number and the honest reason."""

    line_no: int
    reason: str


@dataclass(frozen=True)
class ParsedStatement:
    lines: list[ParsedLine] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    #: dated rows without amounts (the statement's own balance lines) —
    #: skipped and counted, never transactions
    informational: int = 0
    currency: str | None = None
    #: signed, credit-positive (money in bank > 0, overdraft < 0)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None


_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_date(raw: str) -> date:
    """The documented statement-date formats: ISO, ``DD/MM/YYYY``,
    ``DD-MM-YYYY`` and ``DD MMM YYYY`` (Indian statements; the day is
    first — a month-first date is a reject, never a guess)."""
    text = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3,9})[ -](\d{4})", text)
            if m:
                d, y = int(m[1]), int(m[3])
                mo = _MONTHS.get(m[2][:3].lower())
                if mo is None:
                    raise ValueError(f"unknown month {m[2]!r}")
            else:
                raise ValueError("not a supported date format")
    return date(y, mo, d)


def _parse_amount(raw: str) -> Decimal:
    """A statement amount: thousands separators, currency marks and
    trailing Dr/Cr stripped; parentheses are negatives."""
    text = raw.strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = re.sub(r"[₹$€£\s]", "", text)
    if re.search(r"(?i)dr\.?$", text):
        negative = True
    text = re.sub(r"(?i),?(cr|dr)\.?$", "", text)
    text = text.replace(",", "").replace("'", "")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("not a finite number")
    return -value if negative else value


#: header aliases, lowercased and punctuation-stripped, matched in order
_DATE_COLS = (
    "date",
    "value date",
    "txn date",
    "transaction date",
    "posting date",
    "value dt",
    "txn dt",
)
_NARRATIVE_COLS = (
    "narrative",
    "description",
    "particulars",
    "details",
    "remarks",
    "transaction details",
    "description remarks",
)
_REFERENCE_COLS = (
    "reference",
    "ref",
    "ref no",
    "refno",
    "transaction ref",
    "chq no",
    "cheque no",
    "cheque number",
    "chq number",
    "utr",
    "chq/ref no",
    "chq./ref.no.",
    "instrument no",
    "ref no/date",
)
_AMOUNT_COLS = ("amount", "signed amount", "transaction amount", "amt")
_WITHDRAWAL_COLS = (
    "withdrawal",
    "withdrawals",
    "debit",
    "debits",
    "dr",
    "withdrawal amount",
    "paid out",
    "debit amount",
    "dr amount",
)
_DEPOSIT_COLS = (
    "deposit",
    "deposits",
    "credit",
    "credits",
    "cr",
    "deposit amount",
    "paid in",
    "credit amount",
    "cr amount",
)
_CHEQUE_COLS = ("chq no", "cheque no", "cheque number", "chq number", "instrument no")


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ]", "", name.strip().lower())).strip()


def _find_col(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for cand in candidates:
        if cand in headers:
            return headers.index(cand)
    return None


def parse_csv(text: str) -> ParsedStatement:
    """A bank-statement CSV: sniffed delimiter, a required header row, and
    either one signed amount column or a withdrawal/deposit pair. Rows that
    do not parse are rejects with their line numbers — the import refuses
    the batch (all-or-nothing), the preview shows every reason."""
    sample = text.lstrip("\ufeff")
    if not sample.strip():
        raise ValidationError("banking: the statement is empty")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # the overwhelmingly common shape; rows reject if wrong
    reader = csv.reader(io.StringIO(sample), dialect)
    rows = list(reader)
    if not rows:
        raise ValidationError("banking: the statement is empty")

    headers = [_norm_header(h) for h in rows[0]]
    date_i = _find_col(headers, _DATE_COLS)
    narrative_i = _find_col(headers, _NARRATIVE_COLS)
    reference_i = _find_col(headers, _REFERENCE_COLS)
    amount_i = _find_col(headers, _AMOUNT_COLS)
    withdrawal_i = _find_col(headers, _WITHDRAWAL_COLS)
    deposit_i = _find_col(headers, _DEPOSIT_COLS)
    cheque_i = _find_col(headers, _CHEQUE_COLS)
    if date_i is None:
        raise ValidationError(
            "banking: the CSV header has no date column "
            f"(looked for: {', '.join(_DATE_COLS[:4])}…) — got {rows[0]}"
        )
    if amount_i is None and (withdrawal_i is None or deposit_i is None):
        raise ValidationError(
            "banking: the CSV header has no amount — need one amount column "
            "or a withdrawal+deposit pair"
        )

    out = ParsedStatement()
    informational = 0
    for idx, row in enumerate(rows[1:], start=2):  # header is line 1
        if not row or all(not cell.strip() for cell in row):
            continue
        try:
            value_date = _parse_date(row[date_i])
        except ValueError as exc:
            out.rejects.append(Reject(idx, f"date {row[date_i]!r}: {exc}"))
            continue
        reference = (row[reference_i].strip() or None) if reference_i is not None else None
        narrative = (row[narrative_i].strip() or None) if narrative_i is not None else None
        cheque_no = (row[cheque_i].strip() or None) if cheque_i is not None else None
        if cheque_no is None:
            cheque_no = _extract_cheque_no(reference, narrative)
        try:
            if amount_i is not None:
                raw = row[amount_i].strip() if amount_i < len(row) else ""
                if not raw:
                    # a dated row without an amount: the statement's own
                    # opening/closing balance lines are informational —
                    # skipped and counted, never a transaction
                    informational += 1
                    continue
                signed = _parse_amount(raw)
                dc, amount = ("C", signed) if signed > 0 else ("D", -signed)
            else:
                w_raw = row[withdrawal_i].strip() if withdrawal_i < len(row) else ""
                d_raw = row[deposit_i].strip() if deposit_i < len(row) else ""
                if not w_raw and not d_raw:
                    informational += 1
                    continue
                w = _parse_amount(w_raw) if w_raw else Decimal(0)
                d = _parse_amount(d_raw) if d_raw else Decimal(0)
                if w and d:
                    out.rejects.append(
                        Reject(idx, "both withdrawal and deposit present — one side per row")
                    )
                    continue
                if w:
                    dc, amount = "D", w
                elif d:
                    dc, amount = "C", d
                else:
                    out.rejects.append(Reject(idx, "no amount on either side"))
                    continue
            if amount <= 0:
                out.rejects.append(Reject(idx, "amount must be > 0"))
                continue
        except (InvalidOperation, ValueError, ArithmeticError, IndexError):
            out.rejects.append(Reject(idx, "amount does not parse"))
            continue
        out.lines.append(
            ParsedLine(
                value_date=value_date,
                dc=dc,
                amount=_money(amount),
                reference=reference,
                narrative=narrative,
                cheque_no=cheque_no,
            )
        )
    return ParsedStatement(lines=out.lines, rejects=out.rejects, informational=informational)


# ------------------------------------------------------------------- MT940
#: one :61: statement line — value date, optional entry date, the C/D/RC/RD
#: mark, an optional single-character funds code before the amount, the
#: amount (comma decimal, dot thousands), then type/reference text.
_MT940_61 = re.compile(
    r"""^
    (?P<vd>\d{6})            # YYMMDD value date
    (?:(?P<ed>\d{4}))?       # MMDD entry date (optional, same year as vd)
    (?P<mark>RC|RD|[CD])     # debit/credit, optionally a reversal mark
    (?:(?P<funds>[A-Z]))?    # optional funds code — one capital letter
    (?P<amount>\d[\d.,]*)    # the amount, comma as decimal separator
    (?P<rest>.*)$""",
    re.VERBOSE,
)
_MT940_BALANCE = re.compile(r"^(?P<mark>[CD])(?P<date>\d{6})(?P<ccy>[A-Z]{3})(?P<amount>[\d.,]+)$")
#: German-structure :86: segment markers (?20..?29)
_MT940_86_SEG = re.compile(r"\?\d{2}")


def _mt940_amount(raw: str) -> Decimal:
    text = raw.strip()
    # MT940: '.' thousands, ',' decimal — normalise then parse
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("not a finite amount")
    return value


def _extract_cheque_no(reference: str | None, narrative: str | None) -> str | None:
    """A cheque number out of a reference or narrative: the explicit word
    first (CHQ/CHEQUE/CHECK + digits), else a lone long digit run in the
    reference. Never invented — None when the text does not carry one."""
    for text in (narrative or "", reference or ""):
        m = re.search(
            r"(?i)\b(?:chq|cheque|check)\.?\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{4,12})",
            text,
        )
        if m:
            return m[1]
    m = re.search(r"(?i)(?:chq|cheque|check)[/:#-](\d{4,12})", reference or "")
    if m:
        return m[1]
    if reference and re.fullmatch(r"\d{4,12}", reference.strip()):
        return reference.strip()
    return None


def _mt940_year(yy: str) -> int:
    year = int(yy)
    return 2000 + year if year < 70 else 1900 + year


def parse_mt940(text: str) -> ParsedStatement:
    """The SWIFT MT940 customer-statement subset: ``:20:``/``:25:``/``:28c:``
    headers, ``:60F:/M:`` opening and ``:62F:/M:`` closing balances, ``:61:``
    lines with their ``:86:`` narratives (``?NN`` segments joined),
    ``RC``/``RD`` reversal marks importing as the flipped direction, and
    amounts in the SWIFT convention (comma decimal). Unknown structure is a
    reject with the tag, never a guess."""
    if not text.strip():
        raise ValidationError("banking: the statement is empty")
    lines: list[ParsedLine] = []
    rejects: list[Reject] = []
    opening: Decimal | None = None
    closing: Decimal | None = None
    currency: str | None = None
    pending: ParsedLine | None = None  # the :61: whose :86: is accumulating

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r").strip()
        if not line or line == "-":
            continue
        if not line.startswith(":"):
            if pending is not None:  # continuation text of the :86: narrative
                _append_narrative(pending, line)
            continue
        tag_match = re.match(r":(\d{2}[A-Z]?):(.*)", line)
        if tag_match is None:
            rejects.append(Reject(idx, f"statement structure not recognised: {line[:40]!r}"))
            continue
        tag, body = tag_match[1], tag_match[2]
        if tag == "61":
            pending = None
            m = _MT940_61.match(body)
            if m is None:
                rejects.append(Reject(idx, f":61: structure not recognised: {body[:40]!r}"))
                continue
            value_date = date(_mt940_year(m["vd"][:2]), int(m["vd"][2:4]), int(m["vd"][4:6]))
            amount = abs(_mt940_amount(m["amount"]))
            if amount <= 0:
                rejects.append(Reject(idx, ":61: amount must be > 0"))
                continue
            # RC (reversal of a credit) posts Dr; RD posts Cr — the flip
            dc = {"C": "C", "D": "D", "RC": "D", "RD": "C"}[m["mark"]]
            rest = (m["rest"] or "").strip()
            ref_match = re.match(r"(?:N[A-Z]{3})?(?P<ref>[^/]{1,16})", rest)
            reference = (ref_match["ref"].strip() if ref_match else "") or None
            if reference == "NONREF":
                reference = None
            pending = ParsedLine(
                value_date=value_date,
                dc=dc,
                amount=_money(amount),
                reference=reference,
                narrative=None,
                cheque_no=_extract_cheque_no(reference, None),
            )
            lines.append(pending)
        elif tag == "86":
            if pending is None:
                rejects.append(Reject(idx, ":86: narrative without a preceding :61:"))
                continue
            _append_narrative(pending, body)
        elif tag[:2] in ("60", "62", "64"):
            pending = None
            m = _MT940_BALANCE.match(body)
            if m is None:
                rejects.append(Reject(idx, f":{tag}: balance structure not recognised"))
                continue
            signed = _mt940_amount(m["amount"]) * (1 if m["mark"] == "C" else -1)
            if tag[:2] == "60":
                opening = signed
                currency = m["ccy"]
            elif tag[:2] == "62":
                closing = signed
    if rejects:
        return ParsedStatement(
            lines=lines,
            rejects=rejects,
            currency=currency,
            opening_balance=opening,
            closing_balance=closing,
        )
    if not lines:
        raise ValidationError(
            "banking: no :61: statement lines found — an MT940 without "
            "transactions is not a statement"
        )
    return ParsedStatement(
        lines=lines,
        currency=currency,
        opening_balance=opening,
        closing_balance=closing,
    )


def _append_narrative(pending: ParsedLine, text: str) -> None:
    merged = _MT940_86_SEG.sub(" ", text)
    merged = re.sub(r"\s+", " ", merged).strip()
    if pending.narrative:
        merged = f"{pending.narrative} {merged}".strip()
    pending.narrative = merged or None
    pending.cheque_no = _extract_cheque_no(pending.reference, pending.narrative)


def parse_statement(fmt: str, text: str) -> ParsedStatement:
    if fmt == "csv":
        return parse_csv(text)
    if fmt == "mt940":
        return parse_mt940(text)
    raise ValidationError(f"banking: format must be 'csv' or 'mt940', got {fmt!r}")


# ------------------------------------------------------------ ledger guards
def resolve_bank_ledger(session: Session, book: Book, ledger_id: int) -> Ledger:
    """The ledger a banking action targets — 404 outside the book, 422 when
    it is not a bank account (the seeded Bank Accounts / Bank OD A/c
    groups, by ancestry: TallyPrime's rule, BRS is for bank ledgers)."""
    ledger = session.get(Ledger, ledger_id)
    if ledger is None or ledger.book_id != book.id:
        raise NotFoundError(f"Ledger {ledger_id} does not exist in book {book.name!r}")
    group = session.get(Group, ledger.group_id)
    while group is not None:
        if group.name in BANK_GROUPS:
            return ledger
        group = session.get(Group, group.parent_id) if group.parent_id else None
    raise ValidationError(
        f"banking: ledger {ledger.name!r} is not a bank account — statements "
        f"import to ledgers under {' or '.join(BANK_GROUPS)}"
    )


def bank_ledgers(session: Session, book: Book) -> list[dict]:
    """Every bank-account ledger of the book (by group ancestry) — the
    pickers' feed. The current balance rides along asset-positive (Dr
    minus Cr — the figure the statement and the reconciliation read, v1.0's
    correction of the v0.23 Cr-positive mirror)."""
    ledgers = session.scalars(
        select(Ledger).where(Ledger.book_id == book.id).order_by(Ledger.name)
    ).all()
    out = []
    for ledger in ledgers:
        group = session.get(Group, ledger.group_id)
        is_bank = False
        while group is not None:
            if group.name in BANK_GROUPS:
                is_bank = True
                break
            group = session.get(Group, group.parent_id) if group.parent_id else None
        if not is_bank:
            continue
        (balance,) = session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (VoucherEntry.dc == "D", VoucherEntry.amount),
                            else_=-VoucherEntry.amount,
                        )
                    ),
                    0,
                )
            ).where(VoucherEntry.ledger_id == ledger.id)
        ).one()
        out.append({"id": ledger.id, "name": ledger.name, "balance": str(_money(balance))})
    return out


def _fingerprint(ledger_id: int, statement_hash: str, line: ParsedLine, occurrence: int) -> str:
    """The line's content identity: the statement's own hash (which
    statement this is) + the line's facts + its occurrence among the
    statement's identical twins. Re-importing the same statement
    reproduces every fingerprint — a clean no-op; a later statement that
    genuinely repeats a transaction carries a different statement hash and
    lands beside the first; the occurrence keeps twins apart."""
    payload = "|".join(
        (
            str(ledger_id),
            statement_hash,
            line.value_date.isoformat(),
            line.dc,
            str(line.amount),
            line.reference or "",
            line.narrative or "",
            str(occurrence),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _statement_hash(ledger_id: int, lines: list[ParsedLine]) -> str:
    payload = "\n".join(
        (
            f"{line.value_date.isoformat()}|{line.dc}|{line.amount}|"
            f"{line.reference or ''}|{line.narrative or ''}"
        )
        for line in lines
    )
    return hashlib.sha256(f"{ledger_id}\n{payload}".encode()).hexdigest()


# ---------------------------------------------------------------- the import
def import_statement(
    session: Session,
    *,
    book: Book,
    ledger: Ledger,
    fmt: str,
    text: str,
    source: str | None = None,
    opening_balance: str | Decimal | None = None,
    closing_balance: str | Decimal | None = None,
    dry_run: bool = False,
) -> dict:
    """Parse, validate, and (unless ``dry_run``) insert a statement —
    all-or-nothing, idempotent by content fingerprint. Returns the receipt
    the UIs show and the ``bank.statement.imported`` event carries."""
    parsed = parse_statement(fmt, text)
    if parsed.rejects:
        head = "; ".join(f"line {r.line_no}: {r.reason}" for r in parsed.rejects[:5])
        more = f" (+{len(parsed.rejects) - 5} more)" if len(parsed.rejects) > 5 else ""
        raise ValidationError(
            f"banking: {len(parsed.rejects)} of {len(parsed.lines) + len(parsed.rejects)} "
            f"rows rejected — nothing imported (all-or-nothing): {head}{more}"
        )
    if not parsed.lines:
        raise ValidationError("banking: the statement carries no transactions")

    if parsed.currency is not None and parsed.currency != book.base_currency:
        raise ValidationError(
            f"banking: the statement is in {parsed.currency} but the book's base "
            f"currency is {book.base_currency} — FX-account reconciliation is not "
            "in this release (ADR-026 records the follow-up)"
        )

    def _num(value: str | Decimal | None, where: str) -> Decimal | None:
        if value is None or isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValidationError(f"banking: {where} must be a number") from None

    opening = _num(opening_balance, "opening_balance")
    if opening is None:
        opening = parsed.opening_balance
    closing = _num(closing_balance, "closing_balance")
    if closing is None:
        closing = parsed.closing_balance

    # The statement's own identity anchors every fingerprint: re-importing
    # the same statement reproduces them all (duplicates, no batch), while
    # a later statement — different content somewhere — that genuinely
    # repeats a transaction hashes differently and lands beside it. Within
    # one statement, identical twins are kept apart by occurrence.
    stmt_hash = _statement_hash(ledger.id, parsed.lines)
    seen_in_batch: dict[tuple, int] = {}
    new_lines: list[BankStatementLine] = []
    duplicates = 0
    for line in parsed.lines:
        quad = (
            line.value_date,
            line.dc,
            str(line.amount),
            line.reference or "",
            line.narrative or "",
        )
        batch_occ = seen_in_batch.get(quad, 0)
        seen_in_batch[quad] = batch_occ + 1
        fingerprint = _fingerprint(ledger.id, stmt_hash, line, batch_occ)
        already = session.execute(
            select(BankStatementLine.id).where(
                BankStatementLine.book_id == book.id,
                BankStatementLine.fingerprint == fingerprint,
            )
        ).scalar_one_or_none()
        if already is not None:
            duplicates += 1
            continue
        new_lines.append(
            BankStatementLine(
                book_id=book.id,
                ledger_id=ledger.id,
                batch_id=0,  # filled after the batch row flushes
                value_date=line.value_date,
                dc=line.dc,
                amount=line.amount,
                reference=line.reference,
                narrative=line.narrative,
                cheque_no=line.cheque_no,
                fingerprint=fingerprint,
            )
        )

    receipt = {
        "ledger_id": ledger.id,
        "ledger_name": ledger.name,
        "format": fmt,
        "source": source,
        "parsed": len(parsed.lines),
        "informational": parsed.informational,
        "new": len(new_lines),
        "duplicates": duplicates,
        "currency": parsed.currency or book.base_currency,
        "opening_balance": str(_money(opening)) if opening is not None else None,
        "closing_balance": str(_money(closing)) if closing is not None else None,
        "dry_run": dry_run,
    }
    if dry_run or not new_lines:
        # a full duplicate imports no batch — the receipt IS the answer
        return receipt

    batch = BankStatementBatch(
        book_id=book.id,
        ledger_id=ledger.id,
        format=fmt,
        source=source,
        currency=parsed.currency or book.base_currency,
        opening_balance=opening,
        closing_balance=closing,
        line_count=len(new_lines),
    )
    session.add(batch)
    session.flush()  # batch.id for the lines
    for row in new_lines:
        row.batch_id = batch.id
        session.add(row)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    receipt["batch_id"] = batch.id
    logger.info(
        "banking: imported %s statement into %s — %d new, %d duplicates (batch %s)",
        fmt,
        ledger.name,
        len(new_lines),
        duplicates,
        batch.id,
    )
    return receipt


# -------------------------------------------------------- the line book fold
@dataclass(frozen=True)
class MatchInfo:
    """The live (or last) match of one statement line, as the folds see it."""

    voucher_id: int
    voucher_no: str
    voucher_date: date
    entry_id: int
    matched_at: str
    matched_by: str | None
    unmatched_at: str | None
    live: bool


@dataclass(frozen=True)
class BankLineRow:
    """One imported statement line with its match state — the sanctioned
    read (plugin API 0.11) and the UIs' list row. Amounts are strings on
    the wire (Law 3): the client never sums."""

    id: int
    batch_id: int
    value_date: date
    dc: str
    amount: str
    reference: str | None
    narrative: str | None
    cheque_no: str | None
    match: MatchInfo | None = None


def bank_lines(session: Session, *, book: Book, ledger_id: int) -> list[BankLineRow]:
    """Every imported line of one bank ledger, oldest first, with the live
    (or last) match riding along — the fold input for the pack reports and
    the line book the UIs list. Book-scoped like every Resolver read."""
    rows = session.execute(
        select(
            BankStatementLine,
            BankLineMatch,
            Voucher.voucher_no,
        )
        .outerjoin(
            BankLineMatch,
            BankLineMatch.statement_line_id == BankStatementLine.id,
        )
        .outerjoin(Voucher, Voucher.id == BankLineMatch.voucher_id)
        .where(
            BankStatementLine.book_id == book.id,
            BankStatementLine.ledger_id == ledger_id,
        )
        .order_by(BankStatementLine.value_date, BankStatementLine.id)
    ).all()
    out: list[BankLineRow] = []
    for line, match, voucher_no in rows:
        info = None
        if match is not None:
            info = MatchInfo(
                voucher_id=match.voucher_id,
                voucher_no=voucher_no or "",
                voucher_date=match.voucher_date,
                entry_id=match.entry_id,
                matched_at=match.matched_at.isoformat(),
                matched_by=match.matched_by,
                unmatched_at=match.unmatched_at.isoformat() if match.unmatched_at else None,
                live=match.unmatched_at is None,
            )
        out.append(
            BankLineRow(
                id=line.id,
                batch_id=line.batch_id,
                value_date=line.value_date,
                dc=line.dc,
                amount=str(_money(line.amount)),
                reference=line.reference,
                narrative=line.narrative,
                cheque_no=line.cheque_no,
                match=info,
            )
        )
    return out


def bank_entries(
    session: Session, *, book: Book, ledger_id: int, status: str = "all"
) -> list[dict]:
    """The bank ledger's own entries with their live-match state — the
    reconcile screen's book side and the manual matchmaker's discovery
    surface (an entry the suggestions engine will not pair — the amounts
    disagree — is still matchable BY NAME here). ``status`` filters
    unmatched / matched / all. Amounts are strings (Law 3)."""
    matched = _live_matched_entry_ids(session, book)
    rows = []
    for e in _bank_entry_rows(session, book, ledger_id):
        is_matched = e.entry_id in matched
        if status == "unmatched" and is_matched:
            continue
        if status == "matched" and not is_matched:
            continue
        rows.append(
            {
                "entry_id": e.entry_id,
                "voucher_id": e.voucher_id,
                "voucher_no": e.voucher_no,
                "voucher_date": e.voucher_date.isoformat(),
                "dc": e.dc,
                "amount": str(_money(e.amount)),
                "narration": e.narration,
                "matched": is_matched,
            }
        )
    return rows


# ----------------------------------------------------------- match / unmatch
def _now() -> datetime:
    return datetime.now(UTC)


def match_line(
    session: Session,
    *,
    book: Book,
    ledger: Ledger,
    statement_line_id: int,
    entry_id: int,
    voucher_id: int,
    voucher_date: date,
    matched_by: str | None = None,
) -> dict:
    """Record the user's reconciliation judgement (ADR-026): this line is
    that entry. One live match per line (409 if one exists — un-match
    first, the honest two-step); the entry must be the bank ledger's own,
    named with its voucher and date (the trigger re-proves all of it)."""
    line = session.get(BankStatementLine, statement_line_id)
    if line is None or line.book_id != book.id or line.ledger_id != ledger.id:
        raise NotFoundError(
            f"Statement line {statement_line_id} does not exist on ledger {ledger.name!r}"
        )
    existing = session.get(BankLineMatch, statement_line_id)
    if existing is not None and existing.unmatched_at is None:
        raise ConflictError(
            f"Statement line {statement_line_id} is already matched to voucher "
            f"{existing.voucher_id} — un-match it first"
        )
    voucher = session.get(Voucher, voucher_id)
    if voucher is None or voucher.book_id != book.id:
        raise NotFoundError(f"Voucher {voucher_id} does not exist in book {book.name!r}")
    if voucher.voucher_date != voucher_date:
        raise ValidationError(
            f"banking: voucher {voucher_id} is dated {voucher.voucher_date}, not "
            f"{voucher_date} — the match must name the entry's own date"
        )
    entry = session.get(VoucherEntry, entry_id)
    if (
        entry is None
        or entry.voucher_id != voucher_id
        or entry.ledger_id != ledger.id
        or entry.voucher_date != voucher_date
    ):
        raise ValidationError(
            f"banking: entry {entry_id} is not a line of voucher {voucher_id} on "
            f"ledger {ledger.name!r} — reconcile within the bank ledger"
        )

    if existing is not None:  # re-match rewrites the row (mutable by design)
        existing.voucher_id = voucher_id
        existing.entry_id = entry_id
        existing.voucher_date = voucher_date
        existing.matched_at = _now()
        existing.matched_by = matched_by
        existing.unmatched_at = None
    else:
        session.add(
            BankLineMatch(
                statement_line_id=statement_line_id,
                book_id=book.id,
                voucher_id=voucher_id,
                entry_id=entry_id,
                voucher_date=voucher_date,
                matched_by=matched_by,
            )
        )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {
        "statement_line_id": statement_line_id,
        "voucher_id": voucher_id,
        "voucher_no": voucher.voucher_no,
        "entry_id": entry_id,
        "voucher_date": voucher_date.isoformat(),
        "matched_by": matched_by,
    }


def unmatch_line(session: Session, *, book: Book, ledger: Ledger, statement_line_id: int) -> dict:
    """Withdraw a match: the row stays (never deleted — the audit is the
    row's own provenance), ``unmatched_at`` says when the judgement was
    withdrawn. 404 when the line has no live match."""
    line = session.get(BankStatementLine, statement_line_id)
    if line is None or line.book_id != book.id or line.ledger_id != ledger.id:
        raise NotFoundError(
            f"Statement line {statement_line_id} does not exist on ledger {ledger.name!r}"
        )
    existing = session.get(BankLineMatch, statement_line_id)
    if existing is None or existing.unmatched_at is not None:
        raise NotFoundError(f"Statement line {statement_line_id} has no live match to withdraw")
    existing.unmatched_at = _now()
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {"statement_line_id": statement_line_id, "unmatched": True}


# ------------------------------------------------------------ the matchmaker
@dataclass(frozen=True)
class BookEntryRow:
    """One bank-ledger entry as the matchmaker and the proof see it (the
    fold's book side; the carrying voucher's number rides along)."""

    entry_id: int
    voucher_id: int
    voucher_no: str
    voucher_date: date
    dc: str
    amount: Decimal
    narration: str | None
    meta: dict


def _bank_entry_rows(session: Session, book: Book, ledger_id: int) -> list[BookEntryRow]:
    rows = session.execute(
        select(VoucherEntry, Voucher.voucher_no, Voucher.meta)
        .join(Voucher, Voucher.id == VoucherEntry.voucher_id)
        .where(
            VoucherEntry.book_id == book.id,
            VoucherEntry.ledger_id == ledger_id,
        )
        .order_by(VoucherEntry.voucher_date, VoucherEntry.id)
    ).all()
    return [
        BookEntryRow(
            entry_id=e.id,
            voucher_id=e.voucher_id,
            voucher_no=no,
            voucher_date=e.voucher_date,
            dc=e.dc,
            amount=e.amount,
            narration=e.narration,
            meta=meta or {},
        )
        for e, no, meta in rows
    ]


def _live_matched_entry_ids(session: Session, book: Book) -> set[int]:
    return {
        int(row[0])
        for row in session.execute(
            select(BankLineMatch.entry_id).where(
                BankLineMatch.book_id == book.id,
                BankLineMatch.unmatched_at.is_(None),
            )
        )
    }


def _tokens(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if not text:
            continue
        for token in re.split(r"[^a-z0-9]+", text.lower()):
            if len(token) >= 3:
                out.add(token)
    return out


def _entry_cheque_no(entry: BookEntryRow) -> str | None:
    """The cheque number a voucher's own banking declaration carries (the
    pack's ``meta.banking.cheque.no``), if any — the strongest evidence a
    matchmaker can hold."""
    banking = entry.meta.get("banking") if isinstance(entry.meta, dict) else None
    if isinstance(banking, dict):
        cheque = banking.get("cheque")
        if isinstance(cheque, dict):
            no = cheque.get("no")
            if no:
                return str(no)
    return None


def _fuzzy_score(line: BankLineRow, entry: BookEntryRow) -> tuple[int, list[str]]:
    """Reference-fuzzy evidence between a line and an entry: cheque number
    (the line's text vs the voucher's own declaration and narration), the
    voucher number, then token overlap. Returns (score, reasons)."""
    score, reasons = 0, []
    line_text = f"{line.reference or ''} {line.narrative or ''}"
    cheque = line.cheque_no or _extract_cheque_no(line.reference, line.narrative)
    if cheque:
        declared = _entry_cheque_no(entry)
        haystacks = (entry.narration or "", entry.voucher_no)
        if cheque == declared or any(cheque in h.replace(" ", "") for h in haystacks):
            score += 15
            reasons.append(f"cheque {cheque}")
    if entry.voucher_no and entry.voucher_no.lower() in line_text.lower():
        score += 10
        reasons.append(f"voucher {entry.voucher_no}")
    overlap = _tokens(line.reference, line.narrative) & _tokens(entry.narration, entry.voucher_no)
    if overlap:
        denom = max(1, len(_tokens(line.reference, line.narrative)))
        gained = int(15 * len(overlap) / denom)
        if gained:
            score += gained
            reasons.append("narration overlap")
    return score, reasons


def suggestions(
    session: Session,
    *,
    book: Book,
    ledger: Ledger,
    window_days: int = DEFAULT_WINDOW_DAYS,
    per_line: int = 5,
    limit: int = 50,
) -> dict:
    """The matchmaker — a pure function of the current state. Candidates:
    unmatched lines × unmatched entries on the same ledger, same side.
    Ranked by exact amount (+50), date proximity within the window, and
    reference-fuzzy evidence. Written NOWHERE: the roadmap's law
    (suggestions never auto-commit) is this function's type signature."""
    if window_days < 0 or window_days > 366:
        raise ValidationError("banking: window must be 0–366 days")
    if per_line < 1 or per_line > 20:
        raise ValidationError("banking: per_line must be 1–20")
    lines = [
        row
        for row in bank_lines(session, book=book, ledger_id=ledger.id)
        if row.match is None or not row.match.live
    ]
    if not lines:
        return {"ledger_id": ledger.id, "window_days": window_days, "lines": []}
    matched_entries = _live_matched_entry_ids(session, book)
    entries = [
        e for e in _bank_entry_rows(session, book, ledger.id) if e.entry_id not in matched_entries
    ]

    out_lines = []
    for row in lines[:limit]:
        line_amount = Decimal(row.amount)
        candidates = []
        for entry in entries:
            if entry.dc == row.dc:
                continue  # a deposit line reconciles to a Dr-bank entry, a payment to a Cr
            days = abs((entry.voucher_date - row.value_date).days)
            fuzzy, why = _fuzzy_score(row, entry)
            if days > window_days and fuzzy < 15:
                continue  # far apart AND no hard reference evidence: not a candidate
            reasons: list[str] = []
            score = 0
            if entry.amount == line_amount:
                score += 50
                reasons.append("amount exact")
            if days <= window_days:
                score += max(0, 20 - 2 * days)
                reasons.append(f"{days}d apart")
            score += fuzzy
            reasons.extend(why)
            if score >= 30:  # below this it is noise, not a suggestion
                candidates.append(
                    {
                        "entry_id": entry.entry_id,
                        "voucher_id": entry.voucher_id,
                        "voucher_no": entry.voucher_no,
                        "voucher_date": entry.voucher_date.isoformat(),
                        "dc": entry.dc,
                        "amount": str(_money(entry.amount)),
                        "narration": entry.narration,
                        "score": score,
                        "reasons": reasons,
                    }
                )
        candidates.sort(key=lambda c: (-c["score"], c["voucher_date"], c["entry_id"]))
        out_lines.append(
            {
                "statement_line_id": row.id,
                "value_date": row.value_date.isoformat(),
                "dc": row.dc,
                "amount": row.amount,
                "reference": row.reference,
                "narrative": row.narrative,
                "cheque_no": row.cheque_no,
                "suggestions": candidates[:per_line],
            }
        )
    return {"ledger_id": ledger.id, "window_days": window_days, "lines": out_lines}


# -------------------------------------------------------- the reconciliation
def reconciliation(
    session: Session,
    *,
    book: Book,
    ledger: Ledger,
    as_of: date,
    statement_balance: Decimal | str | None = None,
) -> dict:
    """Book vs bank, as-of, with the proof. The identity (exact Decimals):

        book_balance − statement_balance
            = Σ unmatched-book effects − Σ unmatched-statement effects
              + Σ matched-pair differences − opening + unexplained

    where ``opening`` is the earliest imported batch's opening balance (a
    row of its own — the statement side carries it before any line) and
    ``unexplained`` is the mass the imported lines and the anchor cannot
    reconcile between themselves (TallyPrime's "difference in opening"
    row, honestly priced): zero when the anchor is the fold or agrees with
    it, ``opening + Σ lines − anchor`` when the caller's figure disagrees
    or no opening was ever imported.

    ``statement_balance`` (the accountant reads it off the statement — the
    classic anchor) wins when given; otherwise it folds from the earliest
    imported batch opening + every line ≤ as_of. Effects are signed
    Cr-positive: an unmatched statement credit raises the books' bank
    balance once entered; an unmatched book credit (a deposit the bank has
    not shown) raises the statement side; and so on."""
    entries = _bank_entry_rows(session, book, ledger.id)
    entries_upto = [e for e in entries if e.voucher_date <= as_of]
    lines = bank_lines(session, book=book, ledger_id=ledger.id)
    lines_upto = [row for row in lines if row.value_date <= as_of]

    balance_given = statement_balance is not None
    if balance_given and not isinstance(statement_balance, Decimal):
        try:
            statement_balance = Decimal(str(statement_balance))
        except (InvalidOperation, ValueError):
            raise ValidationError("banking: statement_balance must be a number") from None

    folded_balance: Decimal | None = None
    earliest_opening: Decimal | None = None
    for batch in session.scalars(
        select(BankStatementBatch)
        .where(BankStatementBatch.ledger_id == ledger.id)
        .order_by(BankStatementBatch.id)
    ):
        if batch.opening_balance is not None:
            earliest_opening = batch.opening_balance
            break
    if earliest_opening is not None:
        signed_lines = sum(
            (Decimal(row.amount) if row.dc == "C" else -Decimal(row.amount) for row in lines_upto),
            Decimal(0),
        )
        folded_balance = _money(earliest_opening + signed_lines)
    if statement_balance is None:
        if folded_balance is None:
            raise ValidationError(
                "banking: no imported statement carries an opening balance — pass "
                "statement_balance (the figure on the statement) to anchor the proof"
            )
        statement_balance = folded_balance

    book_balance = _money(
        sum(
            (e.amount if e.dc == "D" else -e.amount for e in entries_upto),
            Decimal(0),
        )
    )

    # the pairs: live matches whose line AND entry are both ≤ as_of (a pair
    # straddling the date is an unmatched bank fact on the line's side)
    entry_by_id = {e.entry_id: e for e in entries_upto}
    pairs: list[tuple[BankLineRow, BookEntryRow]] = []
    for row in lines_upto:
        if row.match is None or not row.match.live:
            continue
        entry = entry_by_id.get(row.match.entry_id)
        if entry is not None:
            pairs.append((row, entry))

    paired_entry_ids = {e.entry_id for _row, e in pairs}
    paired_line_ids = {row.id for row, _e in pairs}

    rows: list[dict] = []
    um_book_total = Decimal("0.00")
    um_stmt_total = Decimal("0.00")
    pair_diff_total = Decimal("0.00")
    reconciled = Decimal("0.00")

    for row in lines_upto:
        if row.id in paired_line_ids:
            continue
        effect = Decimal(row.amount) if row.dc == "C" else -Decimal(row.amount)
        um_stmt_total += effect
        rows.append(
            {
                "kind": "stmt_credit" if row.dc == "C" else "stmt_debit",
                "label": (
                    "Credit in bank, not in books"
                    if row.dc == "C"
                    else "Payment per bank, not in books"
                ),
                "date": row.value_date.isoformat(),
                "amount": row.amount,
                "effect": str(_money(effect)),
                "reference": row.reference,
                "narration": row.narrative,
            }
        )
    for entry in entries_upto:
        if entry.entry_id in paired_entry_ids:
            continue
        effect = entry.amount if entry.dc == "D" else -entry.amount
        um_book_total += effect
        rows.append(
            {
                "kind": "book_deposit_not_credited"
                if entry.dc == "D"
                else "cheque_issued_not_presented",
                "label": (
                    "Deposit in books, not yet in the bank"
                    if entry.dc == "D"
                    else "Cheque issued / payment not yet presented"
                ),
                "date": entry.voucher_date.isoformat(),
                "amount": str(_money(entry.amount)),
                "effect": str(_money(effect)),
                "reference": entry.voucher_no,
                "narration": entry.narration,
            }
        )
    for row, entry in pairs:
        reconciled += entry.amount if entry.dc == "D" else -entry.amount
        entry_effect = entry.amount if entry.dc == "D" else -entry.amount
        line_effect = Decimal(row.amount) if row.dc == "C" else -Decimal(row.amount)
        diff = _money(entry_effect - line_effect)
        if diff != 0:
            pair_diff_total += diff
            rows.append(
                {
                    "kind": "pair_difference",
                    "label": "Matched pair priced differently by the two sides",
                    "date": row.value_date.isoformat(),
                    "amount": str(abs(diff)),
                    "effect": str(diff),
                    "reference": row.match.voucher_no if row.match else "",
                    "narration": (
                        f"book {entry.amount} vs bank {row.amount} — line {row.id} "
                        f"↔ voucher {row.match.voucher_no if row.match else ''}"
                    ),
                }
            )

    # the statement side's baseline rows: the opening the statement itself
    # carried, and the mass the lines + anchor cannot explain between
    # themselves (zero when the anchor IS the fold or agrees with it)
    opening_effect = -earliest_opening if earliest_opening is not None else Decimal("0.00")
    if earliest_opening is not None and earliest_opening != 0:
        rows.append(
            {
                "kind": "statement_opening",
                "label": "Opening balance as per the bank statement",
                "date": as_of.isoformat(),
                "amount": str(_money(abs(earliest_opening))),
                "effect": str(_money(opening_effect)),
                "reference": "",
                "narration": "the statement side carries it before any line",
            }
        )
    lines_total = sum(
        (Decimal(row.amount) if row.dc == "C" else -Decimal(row.amount) for row in lines_upto),
        Decimal(0),
    )
    if balance_given:
        unexplained = _money(
            (earliest_opening or Decimal(0)) + lines_total - Decimal(statement_balance)
        )
    else:
        unexplained = Decimal("0.00")
    if unexplained != 0:
        rows.append(
            {
                "kind": "statement_unexplained",
                "label": "Statement balance not explained by the imported lines",
                "date": as_of.isoformat(),
                "amount": str(abs(unexplained)),
                "effect": str(unexplained),
                "reference": "",
                "narration": (
                    "the given balance, the imported opening and the lines "
                    "disagree — import the missing statements or check the figure"
                ),
            }
        )

    lhs = _money(book_balance - Decimal(statement_balance))
    rhs = _money(um_book_total - um_stmt_total + pair_diff_total + opening_effect + unexplained)
    if lhs != rhs:
        raise InvariantViolation(
            "banking: the reconciliation proof failed — book−statement "
            f"{lhs} ≠ unmatched net + pair differences + baseline {rhs}; "
            "this is a bug, not an accounting difference"
        )

    source = "given" if balance_given else "folded"
    if balance_given and folded_balance is not None:
        source = (
            "given (fold agrees)"
            if folded_balance == _money(Decimal(statement_balance))
            else "given (fold disagrees — check the statement period)"
        )
    return {
        "ledger_id": ledger.id,
        "ledger_name": ledger.name,
        "as_of": as_of.isoformat(),
        "book_balance": str(book_balance),
        "statement_balance": str(_money(Decimal(statement_balance))),
        "statement_balance_source": source,
        "reconciled_balance": str(_money(reconciled)),
        "unmatched_statement_total": str(_money(um_stmt_total)),
        "unmatched_book_total": str(_money(um_book_total)),
        "pair_difference_total": str(_money(pair_diff_total)),
        "rows": rows,
        "proof": {"lhs": str(lhs), "rhs": str(rhs), "ok": True},
    }
