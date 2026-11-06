"""Chart-of-accounts seeding (ADR-009, migration 0004).

One definition, two consumers: ``python -m ledgerforge.bootstrap`` (the
default book) and ``POST /api/v1/books`` (every new book) create identical
books from this module — the former SQL seed file is retired with 0004.

The content mirrors TallyPrime's defaults (docs/analysis.md §2.2): the 28
predefined groups (15 primary + 13 sub-groups), the system ledgers
(including the FX gain/loss quartet from ADR-007) and the 10 voucher types.
``nature`` decides Balance Sheet vs P&L placement; ``affects_gross`` drives
the trading-account (gross profit) split. The root "Primary" is
liability-natured so ledgers attached directly to it (like "Profit & Loss
A/c") surface on the liabilities side, matching TallyPrime's display.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Book, Currency, Group, Ledger, VoucherType

DEFAULT_BOOK_SLUG = "default"
DEFAULT_BOOK_NAME = "My Company"
DEFAULT_BASE_CURRENCY = "INR"
DEFAULT_FY_START_MONTH = 4

#: (name, nature, affects_gross) — TallyPrime's 15 primary groups.
PRIMARY_GROUPS: tuple[tuple[str, str, bool], ...] = (
    ("Capital Account", "equity", False),
    ("Loans (Liability)", "liability", False),
    ("Current Liabilities", "liability", False),
    ("Fixed Assets", "asset", False),
    ("Investments", "asset", False),
    ("Current Assets", "asset", False),
    ("Branch / Divisions", "liability", False),
    ("Misc. Expenses (Asset)", "asset", False),
    ("Suspense A/c", "liability", False),
    ("Sales Accounts", "income", True),
    ("Purchase Accounts", "expense", True),
    ("Direct Incomes", "income", True),
    ("Indirect Incomes", "income", False),
    ("Direct Expenses", "expense", True),
    ("Indirect Expenses", "expense", False),
)

#: (name, parent, nature, affects_gross) — the 13 predefined sub-groups.
SUB_GROUPS: tuple[tuple[str, str, str, bool], ...] = (
    ("Reserves & Surplus", "Capital Account", "equity", False),
    ("Bank OD A/c", "Loans (Liability)", "liability", False),
    ("Secured Loans", "Loans (Liability)", "liability", False),
    ("Unsecured Loans", "Loans (Liability)", "liability", False),
    ("Duties & Taxes", "Current Liabilities", "liability", False),
    ("Provisions", "Current Liabilities", "liability", False),
    ("Sundry Creditors", "Current Liabilities", "liability", False),
    ("Bank Accounts", "Current Assets", "asset", False),
    ("Cash-in-Hand", "Current Assets", "asset", False),
    ("Deposits (Asset)", "Current Assets", "asset", False),
    ("Loans & Advances (Asset)", "Current Assets", "asset", False),
    ("Stock-in-Hand", "Current Assets", "asset", False),
    ("Sundry Debtors", "Current Assets", "asset", False),
)

#: (name, parent) — system ledgers created with every book.
SYSTEM_LEDGERS: tuple[tuple[str, str], ...] = (
    ("Cash", "Cash-in-Hand"),
    ("Profit & Loss A/c", "Primary"),
    # FX gain/loss (issue #4, ADR-007): realized differences on
    # settlement/receipt; unrealized on period-end revaluation.
    ("Realized Exchange Gain", "Indirect Incomes"),
    ("Realized Exchange Loss", "Indirect Expenses"),
    ("Unrealized Exchange Gain", "Indirect Incomes"),
    ("Unrealized Exchange Loss", "Indirect Expenses"),
)

#: (code, name, prefix, restart_yearly, is_system) — TallyPrime's set.
VOUCHER_TYPES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("OPEN", "Opening Journal", "OPN", True, True),
    ("FYC", "Financial Year Close", "FYC", True, True),
    ("CN", "Contra", "CN", True, False),
    ("PYT", "Payment", "PYT", True, False),
    ("RCT", "Receipt", "RCT", True, False),
    ("JV", "Journal", "JV", True, False),
    ("SAL", "Sales", "SAL", True, False),
    ("PUR", "Purchase", "PUR", True, False),
    ("CRN", "Credit Note", "CRN", True, False),
    ("DBN", "Debit Note", "DBN", True, False),
)


def ensure_base_currency(session: Session, code: str = DEFAULT_BASE_CURRENCY) -> None:
    """The book's base currency ships as a system currency row."""
    exists = session.scalar(select(Currency.code).where(Currency.code == code))
    if exists is None:
        session.add(
            Currency(
                code=code,
                name="Indian Rupee" if code == DEFAULT_BASE_CURRENCY else code,
                symbol="₹" if code == DEFAULT_BASE_CURRENCY else None,
                minor_unit=2,
                is_system=True,
            )
        )
        session.flush()


def ensure_default_book(session: Session) -> Book:
    """The bootstrap book (slug='default'), created on first run."""
    book = session.scalar(select(Book).where(Book.slug == DEFAULT_BOOK_SLUG))
    if book is None:
        ensure_base_currency(session)
        book = Book(
            name=DEFAULT_BOOK_NAME,
            slug=DEFAULT_BOOK_SLUG,
            base_currency=DEFAULT_BASE_CURRENCY,
            fy_start_month=DEFAULT_FY_START_MONTH,
        )
        session.add(book)
        session.flush()  # id before the seed below
    return book


def seed_book(session: Session, book: Book) -> Book:
    """Idempotently create the TallyPrime defaults inside ``book``:
    root + 28 groups (closure follows by trigger), system ledgers, voucher
    types. Safe to re-run; existing masters are never touched."""
    existing = set(session.scalars(select(Group.name).where(Group.book_id == book.id)).all())

    def _add_group(name: str, parent_id: int | None, nature: str, affects_gross: bool) -> Group:
        group = Group(
            book_id=book.id,
            name=name,
            parent_id=parent_id,
            nature=nature,
            affects_gross=affects_gross,
        )
        session.add(group)
        session.flush()  # closure triggers fire per row (ADR-001)
        existing.add(name)
        return group

    if "Primary" not in existing:
        root = Group(
            book_id=book.id, name="Primary", parent_id=None, nature="liability", is_root=True
        )
        session.add(root)
        session.flush()
        existing.add("Primary")
    else:
        root = session.scalar(
            select(Group).where(Group.book_id == book.id, Group.is_root.is_(True))
        )
        assert root is not None

    by_name = {g.name: g for g in session.scalars(select(Group).where(Group.book_id == book.id))}

    for name, nature, affects_gross in PRIMARY_GROUPS:
        if name not in existing:
            _add_group(name, root.id, nature, affects_gross)
            by_name[name] = session.scalar(
                select(Group).where(Group.book_id == book.id, Group.name == name)
            )

    for name, parent, nature, affects_gross in SUB_GROUPS:
        if name not in existing:
            _add_group(name, by_name[parent].id, nature, affects_gross)
            by_name[name] = session.scalar(
                select(Group).where(Group.book_id == book.id, Group.name == name)
            )

    for name, parent in SYSTEM_LEDGERS:
        found = session.scalar(
            select(Ledger.id).where(Ledger.book_id == book.id, Ledger.name == name)
        )
        if found is None:
            session.add(
                Ledger(book_id=book.id, name=name, group_id=by_name[parent].id, is_system=True)
            )

    type_codes = set(
        session.scalars(select(VoucherType.code).where(VoucherType.book_id == book.id)).all()
    )
    for code, name, prefix, restart_yearly, is_system in VOUCHER_TYPES:
        if code not in type_codes:
            session.add(
                VoucherType(
                    book_id=book.id,
                    code=code,
                    name=name,
                    number_prefix=prefix,
                    restart_yearly=restart_yearly,
                    is_system=is_system,
                )
            )

    session.commit()
    session.refresh(book)
    return book


def bootstrap_default_book(session: Session) -> Book:
    """The bootstrap path: default book + its chart of accounts."""
    book = ensure_default_book(session)
    return seed_book(session, book)
