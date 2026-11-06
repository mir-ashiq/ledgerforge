"""FastAPI routers."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, auth_context, require_role
from ..db import get_session
from ..models import Book

# Modern FastAPI dependency idiom (keeps ruff's B008 happy).
SessionDep = Annotated[Session, Depends(get_session)]

# Role gates (issue #1): reader = read-only, bookkeeper = writes,
# admin = bookkeeper + book-wide control (period locking, FY close).
ReaderDep = Annotated[Principal, Depends(require_role("reader"))]
BookkeeperDep = Annotated[Principal, Depends(require_role("bookkeeper"))]
AdminDep = Annotated[Principal, Depends(require_role("admin"))]

#: The header clients carry the selected book in (ADR-009 — TallyPrime's
#: "select company", made a header).
BOOK_HEADER = "X-Book-Id"


async def book_context(request: Request, session: SessionDep) -> Book:
    """Resolve the book every book-scoped route acts on (ADR-009).

    Resolution rules: the ``X-Book-Id`` header wins; with exactly one
    active book the header is optional (every single-book-era client and
    test keeps working unchanged); zero books is a 400 asking for one;
    two or more active books and no header is a 400 listing the books to
    pick from. Per-book permission claims are enforced here too — the one
    seam (see auth.require_book_access).
    """
    from ..auth import require_book_access

    principal = await auth_context(request)
    raw = request.headers.get(BOOK_HEADER)

    active = session.scalars(
        select(Book).where(Book.is_archived.is_(False)).order_by(Book.id)
    ).all()

    if raw is None:
        if not active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="no books exist yet — create one with POST /api/v1/books",
            )
        if len(active) == 1:
            book = active[0]
        else:
            listing = ", ".join(f"{b.id}:{b.name!r}" for b in active)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("several active books — send X-Book-Id with one of: " + listing),
            )
    else:
        try:
            book_id = int(raw.strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{BOOK_HEADER} must be a book id (integer), got {raw!r}",
            ) from None
        # An explicit header may select an archived book (auditors read
        # history); the no-header path above only auto-picks active ones.
        book = session.get(Book, book_id)
        if book is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"book {book_id} does not exist",
            )

    require_book_access(principal, book.id)
    return book


#: The resolved book for every book-scoped route (ADR-009).
BookDep = Annotated[Book, Depends(book_context)]
