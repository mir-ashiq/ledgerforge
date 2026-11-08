"""Books API: one database, many companies (ADR-009, migration 0004).

A book is a legal entity's complete accounting context — its chart of
accounts, voucher types, numbering, periods, base currency and
fiscal-year start. ``POST /books`` creates one and seeds the TallyPrime
defaults through the same ``ledgerforge.seeding`` definition the CLI
bootstrap uses; every other book-scoped route resolves the book from the
``X-Book-Id`` header (see api.book_context).

Book management is admin territory: creating, renaming or archiving a
company is not bookkeeping.
"""

from __future__ import annotations

import re
import unicodedata

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import exporting
from ..engine.currency import get_currency
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import Book, Group, Ledger, Voucher, VoucherType
from ..schemas import BookCreate, BookOut, BookUpdate
from ..seeding import seed_book
from . import AdminDep, ReaderDep, SessionDep

router = APIRouter(prefix="/books", tags=["books"])


class SeededOut(BaseModel):
    """What POST /books returns: the book plus proof of what was seeded."""

    book: BookOut
    seeded_groups: int
    seeded_ledgers: int
    seeded_voucher_types: int


def _book_out(book: Book) -> BookOut:
    return BookOut(
        id=book.id,
        name=book.name,
        slug=book.slug,
        base_currency=book.base_currency,
        fy_start_month=book.fy_start_month,
        is_archived=book.is_archived,
        created_at=book.created_at,
    )


def _slugify(name: str) -> str:
    """A stable machine key from a display name: ascii-fold, lowercase,
    non-alphanumerics to '-', trimmed of dashes."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug or "book"


def _unique_slug(session: Session, desired: str) -> str:
    """``desired``, or ``desired-2``, ``desired-3`` … — never a collision."""
    slug = desired
    suffix = 1
    while session.scalar(select(Book.id).where(Book.slug == slug)) is not None:
        suffix += 1
        slug = f"{desired}-{suffix}"
    return slug


def _has_vouchers(session: Session, book_id: int) -> bool:
    return (
        session.scalar(select(func.count()).select_from(Voucher).where(Voucher.book_id == book_id))
        or 0
    ) > 0


@router.post("", response_model=SeededOut, status_code=201)
def create_book(payload: BookCreate, session: SessionDep, _: AdminDep):
    """Create a book and seed its chart of accounts in one action —
    TallyPrime's "create company", the multi-book gate (ADR-009)."""
    # The base currency must exist as reference data first (currencies are
    # global ISO 4217 rows, shared across books); a clear 404 beats an FK 500.
    get_currency(session, payload.base_currency)

    slug = _unique_slug(session, payload.slug or _slugify(payload.name))
    if session.scalar(select(Book.id).where(Book.name == payload.name)) is not None:
        raise ValidationError(f"Book name {payload.name!r} already exists")

    book = Book(
        name=payload.name,
        slug=slug,
        base_currency=payload.base_currency,
        fy_start_month=payload.fy_start_month,
    )
    session.add(book)
    session.flush()  # id before the seed
    book = seed_book(session, book)  # groups + ledgers + voucher types

    seeded_groups = (
        session.scalar(select(func.count()).select_from(Group).where(Group.book_id == book.id)) or 0
    )
    seeded_ledgers = (
        session.scalar(select(func.count()).select_from(Ledger).where(Ledger.book_id == book.id))
        or 0
    )
    seeded_voucher_types = (
        session.scalar(
            select(func.count()).select_from(VoucherType).where(VoucherType.book_id == book.id)
        )
        or 0
    )
    return SeededOut(
        book=_book_out(book),
        seeded_groups=seeded_groups,
        seeded_ledgers=seeded_ledgers,
        seeded_voucher_types=seeded_voucher_types,
    )


@router.get("", response_model=list[BookOut])
def list_books(request: Request, session: SessionDep, _: ReaderDep):
    """Every book, archived last — the picker list (and the remedy for the
    400 that asks you to choose)."""
    books = session.scalars(select(Book).order_by(Book.is_archived, Book.id)).all()
    out = [_book_out(b) for b in books]
    exported = exporting.list_export(request, out, BookOut, "books")
    return exported if exported is not None else out


@router.get("/{book_id}", response_model=BookOut)
def get_book(book_id: int, session: SessionDep, _: ReaderDep):
    book = session.get(Book, book_id)
    if book is None:
        raise NotFoundError(f"Book {book_id} does not exist")
    return _book_out(book)


@router.patch("/{book_id}", response_model=BookOut)
def update_book(book_id: int, payload: BookUpdate, session: SessionDep, _: AdminDep):
    """Rename / archive a book. ``base_currency`` and ``fy_start_month``
    refuse to change once the book has vouchers — amounts live in the base
    currency, and changing the FY start would renumber history (Law 2)."""
    book = session.get(Book, book_id)
    if book is None:
        raise NotFoundError(f"Book {book_id} does not exist")

    if payload.name is not None and payload.name != book.name:
        if session.scalar(select(Book.id).where(Book.name == payload.name)) is not None:
            raise ValidationError(f"Book name {payload.name!r} already exists")
        book.name = payload.name

    if payload.is_archived is not None:
        book.is_archived = payload.is_archived

    if (payload.base_currency is not None and payload.base_currency != book.base_currency) or (
        payload.fy_start_month is not None and payload.fy_start_month != book.fy_start_month
    ):
        if _has_vouchers(session, book.id):
            raise ConflictError(
                f"Book {book.name!r} already has vouchers — its base currency and "
                "fiscal-year start are immutable (changing either would restate "
                "history, which the append-only law forbids)"
            )
        if payload.base_currency is not None:
            get_currency(session, payload.base_currency)
            book.base_currency = payload.base_currency
        if payload.fy_start_month is not None:
            book.fy_start_month = payload.fy_start_month

    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        message = str(getattr(exc, "orig", exc))
        if "duplicate key" in message.lower():
            raise ConflictError("book name or slug already exists") from exc
        raise
    session.refresh(book)
    return _book_out(book)
