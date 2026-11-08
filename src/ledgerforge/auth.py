"""Authentication and authorization (issue #1).

Two modes, selected with ``LF_AUTH_MODE``:

``disabled`` (default — development only)
    Every endpoint is open and the principal is an anonymous admin. The API
    stays closed-world in practice because this mode must never be exposed to
    untrusted networks (a loud warning is logged at startup).

``oidc`` (production)
    Requests carry ``Authorization: Bearer <jwt>``; tokens are verified
    against the configured OIDC issuer (Keycloak, Auth0, …) via its JWKS.
    Roles come from token claims and every route demands a minimum role.

Role model (hierarchical)::

    reader      GET masters / vouchers / reports
    bookkeeper  reader + create groups & ledgers, post / reverse vouchers
    admin       bookkeeper + everything else (server management when it lands)

The API layer is the *enforcement point*; the database role
``ledgerforge_app`` (SELECT + INSERT-only on financial history) remains as
defense in depth — see docs/analysis.md §7 and db/schema.sql.

Per-book permissions (ADR-009, migration 0004) ride on the ``books`` claim:
either ``["*"]`` or a list of book ids the principal may touch at its
global role. ``admin`` always passes; a token with no ``books`` claim sees
every book (the single-book-era contract every deployed token already
satisfies). :func:`require_book_access` is the one enforcement seam — a
future membership table can replace the claim reader without touching a
route.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
import jwt as pyjwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from .settings import get_settings

logger = logging.getLogger("ledgerforge.auth")

Role = Literal["reader", "bookkeeper", "admin"]

#: Higher wins; the hierarchy is the whole authorization model.
ROLE_LEVEL: dict[str, int] = {"reader": 0, "bookkeeper": 1, "admin": 2}

#: Claim paths checked for roles, in order: plain ``roles`` (Auth0 custom,
#: most IdPs) then Keycloak's ``realm_access.roles``.
DEFAULT_ROLE_CLAIMS = ("roles", "realm_access.roles")

#: The personal-token prefix (ADR-030): bearers starting with this verify
#: against the database; everything else goes to OIDC.
PERSONAL_TOKEN_PREFIX = "lft_"

_JWKS_CACHE_TTL = 15 * 60  # seconds; refreshes sooner on unknown `kid`


@dataclass(frozen=True)
class Principal:
    """The authenticated caller (or the dev-mode anonymous admin)."""

    sub: str
    roles: tuple[Role, ...]
    claims: dict[str, Any] = field(default_factory=dict)

    def has(self, minimum: Role) -> bool:
        return any(ROLE_LEVEL.get(role, -1) >= ROLE_LEVEL[minimum] for role in self.roles)


ANONYMOUS_ADMIN = Principal(sub="anonymous", roles=("admin",))


class VerificationError(Exception):
    """A token failed verification — always surfaces as HTTP 401."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _extract_roles(claims: dict[str, Any], claim_paths: tuple[str, ...]) -> tuple[Role, ...]:
    """Flatten the configured role claims into normalized role tuples."""
    found: list[str] = []
    for path in claim_paths:
        node: Any = claims
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, (list, tuple)):
            found.extend(str(item) for item in node)
    roles: list[Role] = []
    for role in found:
        normalized = role.strip().lower()
        if normalized in ROLE_LEVEL and normalized not in roles:
            roles.append(normalized)  # type: ignore[arg-type]
    return tuple(roles)


class TokenVerifier(Protocol):
    """Verifies bearer tokens into principals."""

    def verify(self, token: str) -> Principal: ...


class OIDCTokenVerifier:
    """JWT verification against an OIDC provider's JWKS.

    Keys are fetched per ``kid`` and cached; an unknown ``kid`` (key
    rotation) triggers exactly one forced refresh. ``http_client`` is
    injectable for tests; production uses a plain :class:`httpx.Client`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        roles_claims: tuple[str, ...] = DEFAULT_ROLE_CLAIMS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_uri = jwks_uri
        self.roles_claims = roles_claims
        self._client = http_client or httpx.Client(timeout=10.0)
        self._keys: dict[str, tuple[Any, float]] = {}
        self._jwks_uri: str | None = jwks_uri

    # ------------------------------------------------------------- jwks
    def _discover_jwks_uri(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        response = self._client.get(f"{self.issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        uri = response.json().get("jwks_uri")
        if not uri:
            raise VerificationError("issuer discovery document has no jwks_uri")
        self._jwks_uri = str(uri)
        return self._jwks_uri

    def _fetch_jwks(self) -> dict[str, Any]:
        response = self._client.get(self._discover_jwks_uri())
        response.raise_for_status()
        return response.json()

    def _key_for(self, kid: str | None, *, force_refresh: bool = False) -> Any:
        now = time.monotonic()
        cached = self._keys.get(kid or "")
        if cached and not force_refresh and cached[1] > now:
            return cached[0]
        jwks = self._fetch_jwks()
        for jwk in jwks.get("keys", []):
            if jwk.get("kid") == kid or kid is None:
                key = self._jwk_to_key(jwk)
                self._keys[jwk.get("kid") or ""] = (key, now + _JWKS_CACHE_TTL)
                if jwk.get("kid") == kid:
                    return key
        if kid is None:
            raise VerificationError("issuer JWKS exposes no usable keys")
        raise VerificationError(f"no key for kid {kid!r} in issuer JWKS")

    @staticmethod
    def _jwk_to_key(jwk: dict[str, Any]) -> Any:
        try:
            kty = jwk.get("kty")
            if kty == "RSA":
                return pyjwt.algorithms.RSAAlgorithm.from_jwk(jwk)
            if kty == "EC":
                return pyjwt.algorithms.ECAlgorithm.from_jwk(jwk)
            raise VerificationError(f"unsupported JWKS key type {kty!r}")
        except VerificationError:
            raise
        except Exception as exc:  # malformed JWK
            raise VerificationError(f"malformed JWK from issuer: {exc}") from exc

    def _decode(self, token: str, key: Any) -> dict[str, Any]:
        return pyjwt.decode(
            token,
            key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=self.audience,
            issuer=self.issuer,
        )

    # ---------------------------------------------------------- verify
    def verify(self, token: str) -> Principal:
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError as exc:
            raise VerificationError("malformed token") from exc
        kid = header.get("kid")

        try:
            key = self._key_for(kid)
        except VerificationError as exc:
            # unknown kid — maybe the IdP rotated keys: one forced refresh
            try:
                key = self._key_for(kid, force_refresh=True)
            except VerificationError:
                raise VerificationError(f"no signing key for kid {kid!r}") from exc

        try:
            claims = self._decode(token, key)
        except pyjwt.InvalidSignatureError:
            # cached key material went stale (same kid, rotated): retry once
            try:
                claims = self._decode(token, self._key_for(kid, force_refresh=True))
            except Exception as exc2:
                raise VerificationError("token signature verification failed") from exc2
        except pyjwt.ExpiredSignatureError as exc:
            raise VerificationError("token expired") from exc
        except pyjwt.InvalidAudienceError as exc:
            raise VerificationError("token audience mismatch") from exc
        except pyjwt.InvalidIssuerError as exc:
            raise VerificationError("token issuer mismatch") from exc
        except pyjwt.PyJWTError as exc:
            raise VerificationError(f"invalid token: {exc}") from exc

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise VerificationError("token has no subject (sub)")
        roles = _extract_roles(claims, self.roles_claims)
        if not roles:
            raise VerificationError("token carries no recognized role")
        return Principal(sub=sub, roles=roles, claims=claims)


# ------------------------------------------------------------ personal tokens


def mint_personal_token() -> tuple[str, str, str]:
    """(raw, prefix, sha256hex) — the raw token is shown exactly once
    (ADR-030). 32 urlsafe chars ≈ 190 bits of entropy: a plain SHA-256
    lookup hash is the honest construction (no salted KDF needed for
    high-entropy random inputs, unlike human passwords)."""
    raw = PERSONAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[:12], hashlib.sha256(raw.encode()).hexdigest()


def hash_personal_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class PersonalTokenVerifier:
    """Verifies ``lft_…`` bearers against ``api_tokens`` (ADR-030): the
    desktop's non-OIDC answer. One indexed lookup by SHA-256, the
    revocation and expiry verdicts, one cheap touch-write for
    ``last_used_at`` (usage is observability, not audit). The session
    factory is injectable for tests; production uses the engine's own.
    """

    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from .db import session_factory as default_factory

            session_factory = default_factory()  # the sessionmaker itself
        self._session_factory = session_factory

    def verify(self, token: str) -> Principal:
        from .models import ApiToken

        digest = hash_personal_token(token)
        session = self._session_factory()
        try:
            row = session.scalar(select(ApiToken).where(ApiToken.token_hash == digest))
            if row is None:
                raise VerificationError("unknown personal token")
            if row.revoked_at is not None:
                raise VerificationError("personal token has been revoked")
            if row.expires_at is not None:
                expires = row.expires_at
                if expires.tzinfo is None:  # a naive column read — treat as UTC
                    expires = expires.replace(tzinfo=UTC)
                if expires <= datetime.now(UTC):
                    raise VerificationError("personal token has expired")
            principal = Principal(
                sub=f"token:{row.id}",
                roles=(row.role,),  # type: ignore[arg-type]
                claims={"books": list(row.books or ["*"]), "token_name": row.name},
            )
            # the touch-write: best effort — a failed UPDATE never fails auth
            try:
                row.last_used_at = datetime.now(UTC)
                session.commit()
            except Exception:  # noqa: BLE001 — observability only
                session.rollback()
                logger.warning("personal token last_used touch failed", exc_info=True)
            return principal
        finally:
            session.close()


# ------------------------------------------------------------ FastAPI layer


def build_verifier() -> OIDCTokenVerifier | None:
    """From settings; None in disabled mode."""
    settings = get_settings()
    if settings.auth_mode == "disabled":
        return None
    return OIDCTokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_uri=settings.oidc_jwks_uri or None,
    )


async def auth_context(request: Request) -> Principal:
    """The authenticated principal — or 401. Every protected route starts here."""
    verifier: TokenVerifier | None = request.app.state.token_verifier
    if verifier is None:  # LF_AUTH_MODE=disabled (dev only)
        return ANONYMOUS_ADMIN
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header[7:].strip()
    # The ADR-030 chain: personal tokens (lft_…) verify against the
    # database, everything else against the OIDC issuer. The prefix makes
    # the dispatch unambiguous — a JWT never starts with "lft_".
    if token.startswith(PERSONAL_TOKEN_PREFIX):
        personal: PersonalTokenVerifier | None = getattr(
            request.app.state, "personal_token_verifier", None
        )
        if personal is None:  # not wired (unit tests without the app)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="personal tokens are not available on this deployment",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return personal.verify(token)
        except VerificationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=exc.detail,
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
    try:
        return verifier.verify(token)
    except VerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_role(minimum: Role):
    """Route dependency demanding at least ``minimum`` (403 below it)."""

    async def dependency(request: Request) -> Principal:
        principal = await auth_context(request)
        if not principal.has(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this action requires the {minimum!r} role or higher",
            )
        return principal

    return dependency


# ------------------------------------------------------- per-book access


def allowed_book_ids(principal: Principal) -> set[int] | None:
    """The book ids this principal may touch, or None for "every book".

    ADR-009: the token may carry a ``books`` claim — either ``["*"]`` or a
    list of book ids. No claim at all means every book: that is the
    single-book-era contract every token issued before 0004 satisfies.
    Malformed claims (non-numeric, non-list) fail closed to *nothing*, not
    open to everything.
    """
    if "admin" in principal.roles:
        return None  # admins see every book, whatever the claim says
    claim = principal.claims.get("books")
    if claim is None:
        return None
    if isinstance(claim, str):
        claim = [claim]
    if not isinstance(claim, (list, tuple)):
        return set()
    ids: set[int] = set()
    for item in claim:
        if item == "*":
            return None
        # ints and integer-looking strings only; floats and other junk are
        # ignored (fail closed — never round 7.5 into book 7)
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            continue
        try:
            ids.add(int(item))
        except ValueError:
            continue
    return ids


def require_book_access(principal: Principal, book_id: int) -> None:
    """The single seam where per-book permissions are enforced (ADR-009).
    Raises 403 when the principal's ``books`` claim excludes ``book_id``.
    Scoping the *role* per book is deliberately left to the IdP issuing
    claims; when a membership table replaces the claim reader, this is the
    only function that changes.
    """
    allowed = allowed_book_ids(principal)
    if allowed is not None and book_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"this token is not authorized for book {book_id} "
                "(its 'books' claim lists other books)"
            ),
        )


def warn_if_disabled() -> None:
    if get_settings().auth_mode == "disabled":
        logger.warning(
            "LF_AUTH_MODE=disabled — authentication is OFF; every caller is an "
            "anonymous admin. Development only: never expose this to untrusted "
            "networks. Set LF_AUTH_MODE=oidc plus LF_OIDC_ISSUER/AUDIENCE for "
            "production."
        )
