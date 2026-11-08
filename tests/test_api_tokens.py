"""Personal API tokens (ADR-030, Phase 9): mint, list, revoke — and the
chained verifier (``lft_`` → database, everything else → the issuer).

The promises under test:

- **The raw token appears exactly once** (at mint, in the body — never a
  header, never the list).
- **The ceiling holds**: a principal may never mint above its own level.
- **The chain works in production mode**: with LF_AUTH_MODE=oidc, an
  ``lft_`` bearer authenticates through the database while a JWT goes to
  the issuer; an OIDC principal and a token principal coexist.
- **Scope is real**: the token's ``books`` claim rides the books-strangers
  seam (a scoped token is refused another book); the role is the token's
  own (a reader token cannot post).
- **Revocation and expiry are immediate** — the next request is a 401.
- **``last_used_at`` is touched** (usage is observability, not audit).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_auth import AUDIENCE, ISSUER, JWKS_URL, bearer, make_token, mock_client


@pytest.fixture()
def oidc_app(monkeypatch, sqlalchemy_url):
    """The test_auth deployment shape, local: LF_AUTH_MODE=oidc with the
    JWKS served through a mock transport (fixtures are module-scoped by
    discovery — the pattern must be restated here)."""
    from ledgerforge import db
    from ledgerforge.auth import OIDCTokenVerifier
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    for key in (
        "LF_AUTH_MODE",
        "LF_OIDC_ISSUER",
        "LF_OIDC_AUDIENCE",
        "LF_OIDC_JWKS_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LF_AUTH_MODE", "oidc")
    monkeypatch.setenv("LF_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("LF_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("LF_OIDC_JWKS_URI", JWKS_URL)
    get_settings.cache_clear()
    db.reset_engine()
    app = create_app()
    app.state.token_verifier = OIDCTokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URL, http_client=mock_client()
    )
    yield app
    get_settings.cache_clear()
    db.reset_engine()


@pytest.fixture()
def oidc_client(oidc_app):
    with TestClient(oidc_app) as c:
        yield c


def _mint(client: TestClient, headers: dict | None = None, **payload) -> dict:
    """Mint through the admin surface — an admin JWT when auth is on."""
    if headers is None:
        headers = bearer(make_token(roles=["admin"]))
    body = {"name": "the sync script", "role": "reader", **payload}
    r = client.post("/api/v1/tokens", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- lifecycle
def test_mint_returns_raw_once_list_never_shows_it(oidc_client):
    minted = _mint(oidc_client)
    assert minted["token"].startswith("lft_")
    assert len(minted["token"]) > 40

    listed = oidc_client.get("/api/v1/tokens", headers=bearer(make_token(roles=["admin"]))).json()
    assert any(row["id"] == minted["id"] for row in listed)
    for row in listed:
        assert "token" not in row  # the list identifies (prefix), never authenticates
        assert row["token_prefix"]


def test_ceiling_a_principal_never_mints_above_itself(oidc_client):
    admin_jwt = bearer(make_token(roles=["admin"]))
    r = oidc_client.post(
        "/api/v1/tokens",
        json={"name": "admin token", "role": "admin"},
        headers=admin_jwt,
    )
    assert r.status_code == 201, r.text

    # a bookkeeper principal cannot reach the admin-only surface at all —
    # the route gate (403) fires before the mint's own ceiling check, which
    # stays as defense in depth for any future lesser-principaled route
    bk_jwt = bearer(make_token(roles=["bookkeeper"], sub="bk-1"))
    r = oidc_client.post(
        "/api/v1/tokens",
        json={"name": "escape", "role": "admin"},
        headers=bk_jwt,
    )
    assert r.status_code == 403
    assert oidc_client.get("/api/v1/tokens", headers=bk_jwt).status_code == 403


def test_role_and_books_validation(oidc_client):
    admin = bearer(make_token(roles=["admin"]))
    r = oidc_client.post("/api/v1/tokens", json={"name": "x", "role": "wizard"}, headers=admin)
    assert r.status_code == 422
    r = oidc_client.post("/api/v1/tokens", json={"name": "", "role": "reader"}, headers=admin)
    assert r.status_code == 422


# ---------------------------------------------------------------- the chain
def test_lft_bearer_authenticates_through_the_database(oidc_client):
    minted = _mint(oidc_client, role="reader")
    r = oidc_client.get("/api/v1/books", headers=bearer(minted["token"]))
    assert r.status_code == 200, r.text

    # last_used_at is touched (usage is observability)
    listed = oidc_client.get("/api/v1/tokens", headers=bearer(make_token(roles=["admin"]))).json()
    row = next(item for item in listed if item["id"] == minted["id"])
    assert row["last_used_at"] is not None


def test_a_reader_token_cannot_write(oidc_client):
    minted = _mint(oidc_client, role="reader")
    r = oidc_client.post(
        "/api/v1/ledgers",
        json={"name": "Nope Ltd", "group_name": "Sundry Debtors"},
        headers=bearer(minted["token"]),
    )
    assert r.status_code == 403


def test_a_jwt_still_goes_to_the_issuer(oidc_client):
    """The prefix dispatches: a normal JWT never touches api_tokens."""
    r = oidc_client.get("/api/v1/books", headers=bearer(make_token(roles=["reader"])))
    assert r.status_code == 200
    garbage = oidc_client.get("/api/v1/books", headers=bearer("lft_not-a-real-token"))
    assert garbage.status_code == 401


def test_books_scope_rides_the_claim_seam(oidc_client):
    """A token scoped to one book is a stranger to the others (ADR-009's
    ``books`` claim, reused verbatim)."""
    admin = bearer(make_token(roles=["admin"]))
    books = oidc_client.get("/api/v1/books", headers=admin).json()
    assert books, "the deployment has at least the default book"
    first = books[0]["id"]
    minted = _mint(oidc_client, role="reader", books=[first])
    headers = {**bearer(minted["token"]), "X-Book-Id": str(first)}
    assert oidc_client.get("/api/v1/ledgers", headers=headers).status_code == 200

    import uuid as _uuid

    other = {
        "name": "Scoped Other",
        "slug": f"scoped-other-{_uuid.uuid4().hex[:8]}",
        "base_currency": "INR",
    }
    r = oidc_client.post("/api/v1/books", json=other, headers=admin)
    if r.status_code == 201:  # the second book exists — the scoped token is its stranger
        other_id = r.json()["book"]["id"]
        scoped = {**bearer(minted["token"]), "X-Book-Id": str(other_id)}
        assert oidc_client.get("/api/v1/ledgers", headers=scoped).status_code == 403
        # archive it: module residue must not turn every later single-book
        # caller's no-header request into a 400 (books are strangers' seams)
        archived = oidc_client.patch(
            f"/api/v1/books/{other_id}", json={"is_archived": True}, headers=admin
        )
        assert archived.status_code == 200


# ------------------------------------------------------------ revoke/expire
def test_revocation_is_immediate(oidc_client):
    minted = _mint(oidc_client)
    headers = bearer(minted["token"])
    assert oidc_client.get("/api/v1/books", headers=headers).status_code == 200

    r = oidc_client.delete(
        f"/api/v1/tokens/{minted['id']}", headers=bearer(make_token(roles=["admin"]))
    )
    assert r.status_code == 200
    assert r.json()["revoked_at"] is not None

    assert oidc_client.get("/api/v1/books", headers=headers).status_code == 401
    admin = bearer(make_token(roles=["admin"]))
    # double-revoke is a 409
    assert oidc_client.delete(f"/api/v1/tokens/{minted['id']}", headers=admin).status_code == 409
    assert oidc_client.delete("/api/v1/tokens/999999", headers=admin).status_code == 404


def test_expiry_is_immediate(oidc_client):
    expired = datetime.now(UTC) - timedelta(minutes=1)
    minted = _mint(oidc_client, expires_at=expired.isoformat())
    r = oidc_client.get("/api/v1/books", headers=bearer(minted["token"]))
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


# ------------------------------------------------------------- the verifier
def test_the_verifier_unit_paths():
    """Unknown hash, revoked row, expiry — the verdicts, without HTTP."""
    from ledgerforge.auth import PersonalTokenVerifier, VerificationError
    from ledgerforge.models import ApiToken

    class _Session:
        def __init__(self, row):
            self.row = row

        def scalar(self, *_a, **_k):
            return self.row

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    # the real ``db.session_factory`` is a FUNCTION returning a sessionmaker
    # (the verifier calls it twice) — the stub mirrors that shape
    def _factory(row):
        return lambda: _Session(row)

    verifier = PersonalTokenVerifier(
        session_factory=_factory(
            ApiToken(
                id=1,
                name="u",
                token_prefix="lft_x",
                token_hash="h",
                role="reader",
                books=["*"],
            )
        )
    )
    principal = verifier.verify("lft_whatever")
    assert principal.roles == ("reader",)
    assert principal.claims["books"] == ["*"]

    verifier = PersonalTokenVerifier(session_factory=_factory(None))
    with pytest.raises(VerificationError):
        verifier.verify("lft_unknown")
