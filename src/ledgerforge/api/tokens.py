"""Personal API tokens (ADR-030, roadmap Phase 9): long-lived, scoped,
revocable credentials — the desktop's non-OIDC answer for integrations.

Admin-only surface. The raw token is returned exactly once at mint and
never stored (the row carries its SHA-256); the role ceiling holds (a
principal may never mint above its own level); the books scope reuses the
``books``-claim semantics verbatim; revocation is a soft verdict the row
remembers. In ``disabled`` auth mode the whole chain is bypassed (every
caller is the anonymous admin) — tokens minted there are usable the day
the deployment turns authentication on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import ROLE_LEVEL, mint_personal_token
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import ApiToken
from . import AdminDep, SessionDep

router = APIRouter(prefix="/tokens", tags=["tokens"])


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="what this token is for")
    role: str = Field(description="reader, bookkeeper or admin — the token's ceiling")
    books: list[int] | None = Field(
        default=None,
        description="the book ids this token may touch; omitted = every book "
        "(the books-claim semantics, ADR-009)",
    )
    expires_at: datetime | None = Field(
        default=None, description="optional expiry; omitted = long-lived"
    )


class TokenOut(BaseModel):
    id: int
    name: str
    token_prefix: str
    role: str
    books: list
    expires_at: datetime | None
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenMinted(TokenOut):
    token: str = Field(description="the raw token — shown exactly once")
    note: str = "shown exactly once — store it now; recovery is re-mint, never recall"


def _token_out(row: ApiToken) -> TokenOut:
    return TokenOut(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        role=row.role,
        books=list(row.books or ["*"]),
        expires_at=row.expires_at,
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


@router.post("", response_model=TokenMinted, status_code=201)
def create_token(payload: TokenCreate, session: SessionDep, principal: AdminDep):
    """Mint a personal token. The raw token appears exactly once in this
    response — store it now; there is no recovery path but revocation and
    re-mint (the security posture, not an oversight)."""
    if payload.role not in ROLE_LEVEL:
        raise ValidationError(
            f"tokens: role must be one of {', '.join(ROLE_LEVEL)}, got {payload.role!r}"
        )
    # The ceiling: a principal may never mint above its own level.
    if not principal.has(payload.role):  # type: ignore[arg-type]
        raise ValidationError(
            f"tokens: you may not mint a {payload.role!r} token — the ceiling is "
            "your own highest role"
        )
    raw, prefix, digest = mint_personal_token()
    row = ApiToken(
        name=payload.name,
        token_prefix=prefix,
        token_hash=digest,
        role=payload.role,
        books=(payload.books if payload.books is not None else ["*"]),
        expires_at=payload.expires_at,
        created_by=principal.sub,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    out = _token_out(row)
    # The one time the raw token exists in a response — riding the body,
    # never a header (headers leak into access logs).
    return TokenMinted(**out.model_dump(), token=raw)


@router.get("", response_model=list[TokenOut])
def list_tokens(session: SessionDep, _: AdminDep):
    """Every token with its prefix (never the hash — the list identifies,
    it does not authenticate)."""
    rows = session.scalars(select(ApiToken).order_by(ApiToken.id.desc())).all()
    return [_token_out(row) for row in rows]


@router.delete("/{token_id}", response_model=TokenOut)
def revoke_token(token_id: int, session: SessionDep, _: AdminDep):
    """Revoke — the row stays (history), ``revoked_at`` is the verdict,
    and the very next request carrying it answers 401."""
    row = session.get(ApiToken, token_id)
    if row is None:
        raise NotFoundError(f"tokens: token {token_id} does not exist")
    if row.revoked_at is not None:
        raise ConflictError(f"tokens: token {token_id} is already revoked")
    row.revoked_at = datetime.now(UTC)
    session.commit()
    session.refresh(row)
    return _token_out(row)
