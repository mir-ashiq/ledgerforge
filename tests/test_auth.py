"""AuthN/AuthZ (issue #1): OIDC bearer verification, the role model, and the
mode gate. Tokens are minted with a throwaway RSA key whose JWKS is served
through an httpx MockTransport — the real verification code path runs, only
the network is fake.
"""

from __future__ import annotations

import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ISSUER = "https://idp.example.test"
AUDIENCE = "ledgerforge"
JWKS_URL = f"{ISSUER}/jwks"
KID = "test-key-1"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = pyjwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True)
PUBLIC_JWK["kid"] = KID
PUBLIC_JWK["alg"] = "RS256"
PRIVATE_PEM = PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

jwks_fetches = 0


def _jwks_handler(request: httpx.Request) -> httpx.Response:
    global jwks_fetches  # noqa: PLW0603
    jwks_fetches += 1
    return httpx.Response(200, json={"keys": [PUBLIC_JWK]})


def mock_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_jwks_handler))


def make_token(
    *,
    roles: list[str] | None = None,
    realm_access: list[str] | None = None,
    sub: str = "user-1",
    aud: str = AUDIENCE,
    iss: str = ISSUER,
    kid: str = KID,
    exp_offset: int = 300,
    algorithm: str = "RS256",
    key: bytes | object = PRIVATE_PEM,
) -> str:
    now = int(time.time())
    claims: dict = {"sub": sub, "iss": iss, "aud": aud, "iat": now, "exp": now + exp_offset}
    if roles is not None:
        claims["roles"] = roles
    if realm_access is not None:
        claims["realm_access"] = {"roles": realm_access}
    return pyjwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def oidc_app(monkeypatch, sqlalchemy_url):
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
    # DI point: the app-built verifier would hit the real network; swap in
    # the same verifier wired to the mock transport.
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


# ------------------------------------------------------------------ 401 paths


def test_no_token_is_401(oidc_client):
    assert oidc_client.get("/api/v1/ledgers").status_code == 401
    assert oidc_client.get("/api/v1/reports/trial-balance").status_code == 401
    assert oidc_client.post("/api/v1/vouchers", json={}).status_code == 401


def test_garbage_token_is_401(oidc_client):
    r = oidc_client.get("/api/v1/ledgers", headers=bearer("not-a-jwt"))
    assert r.status_code == 401


def test_expired_token_is_401(oidc_client):
    r = oidc_client.get(
        "/api/v1/ledgers", headers=bearer(make_token(roles=["reader"], exp_offset=-60))
    )
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_wrong_audience_is_401(oidc_client):
    r = oidc_client.get(
        "/api/v1/ledgers", headers=bearer(make_token(roles=["reader"], aud="other-app"))
    )
    assert r.status_code == 401
    assert "audience" in r.json()["detail"]


def test_wrong_issuer_is_401(oidc_client):
    r = oidc_client.get(
        "/api/v1/ledgers", headers=bearer(make_token(roles=["reader"], iss="https://evil.test"))
    )
    assert r.status_code == 401
    assert "issuer" in r.json()["detail"]


def test_token_without_any_role_is_401(oidc_client):
    r = oidc_client.get("/api/v1/ledgers", headers=bearer(make_token()))
    assert r.status_code == 401
    assert "role" in r.json()["detail"]


def test_unknown_kid_is_401_after_one_refresh(oidc_client):
    global jwks_fetches
    before = jwks_fetches
    r = oidc_client.get(
        "/api/v1/ledgers", headers=bearer(make_token(roles=["reader"], kid="rotated-away"))
    )
    assert r.status_code == 401
    assert jwks_fetches == before + 2  # initial lookup + exactly one refresh


def test_alg_confusion_rejected(oidc_client):
    """A token signed HS256 with the *public* key as HMAC secret must fail:
    the algorithm whitelist is the defense (classic JWT pitfall)."""
    import json

    token = make_token(roles=["admin"], algorithm="HS256", key=json.dumps(PUBLIC_JWK))
    r = oidc_client.get("/api/v1/ledgers", headers=bearer(token))
    assert r.status_code == 401


def test_health_stays_public(oidc_client):
    assert oidc_client.get("/health").status_code == 200


# ----------------------------------------------------------------- role model


def test_reader_can_read_but_not_write(oidc_client):
    token = bearer(make_token(roles=["reader"]))
    assert oidc_client.get("/api/v1/ledgers", headers=token).status_code == 200
    assert oidc_client.get("/api/v1/groups", headers=token).status_code == 200
    assert oidc_client.get("/api/v1/vouchers", headers=token).status_code == 200
    assert oidc_client.get("/api/v1/reports/trial-balance", headers=token).status_code == 200
    assert (
        oidc_client.post(
            "/api/v1/ledgers", headers=token, json={"name": "X", "group_name": "Cash-in-Hand"}
        ).status_code
        == 403
    )
    r = oidc_client.post(
        "/api/v1/vouchers",
        headers=token,
        json={"voucher_type": "JV", "voucher_date": "2026-01-01", "entries": []},
    )
    assert r.status_code == 403


def test_bookkeeper_can_post_ledgers_and_vouchers(oidc_client):
    token = bearer(make_token(roles=["bookkeeper"]))
    r = oidc_client.post(
        "/api/v1/ledgers",
        headers=token,
        json={"name": "Auth Test Cash", "group_name": "Cash-in-Hand"},
    )
    assert r.status_code == 201, r.text
    r = oidc_client.post(
        "/api/v1/ledgers",
        headers=token,
        json={"name": "Auth Test Sales", "group_name": "Sales Accounts"},
    )
    assert r.status_code == 201, r.text

    r = oidc_client.post(
        "/api/v1/vouchers",
        headers=token,
        json={
            "voucher_type": "SAL",
            "voucher_date": "2026-01-05",
            "entries": [
                {"ledger_name": "Auth Test Cash", "dc": "D", "amount": "118.00"},
                {"ledger_name": "Auth Test Sales", "dc": "C", "amount": "118.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["voucher_no"].startswith("SAL-")


def test_admin_passes_everything(oidc_client):
    token = bearer(make_token(roles=["admin"]))
    assert oidc_client.get("/api/v1/ledgers", headers=token).status_code == 200
    assert (
        oidc_client.post(
            "/api/v1/ledgers",
            headers=token,
            json={"name": "Admin Ledger", "group_name": "Cash-in-Hand"},
        ).status_code
        == 201
    )


def test_role_hierarchy_reader_endpoints_accept_higher_roles(oidc_client):
    for roles in (["bookkeeper"], ["admin"], ["reader", "bookkeeper"]):
        r = oidc_client.get("/api/v1/auth/me", headers=bearer(make_token(roles=roles)))
        assert r.status_code == 200, roles


def test_keycloak_realm_access_roles_honored(oidc_client):
    token = bearer(make_token(realm_access=["reader"]))  # Keycloak-style claim
    assert oidc_client.get("/api/v1/ledgers", headers=token).status_code == 200
    assert (
        oidc_client.post(
            "/api/v1/ledgers", headers=token, json={"name": "K", "group_name": "Cash-in-Hand"}
        ).status_code
        == 403
    )


def test_roles_are_normalized(oidc_client):
    r = oidc_client.get("/api/v1/auth/me", headers=bearer(make_token(roles=["READER", " reader "])))
    assert r.status_code == 200
    assert r.json()["roles"] == ["reader"]


def test_me_reports_subject_roles_and_mode(oidc_client):
    r = oidc_client.get(
        "/api/v1/auth/me", headers=bearer(make_token(sub="alice", roles=["bookkeeper"]))
    )
    assert r.json() == {"sub": "alice", "roles": ["bookkeeper"], "auth_mode": "oidc"}


def test_jwks_is_fetched_once_then_cached(oidc_client):
    global jwks_fetches
    before = jwks_fetches
    token = bearer(make_token(roles=["reader"]))
    for _ in range(3):
        assert oidc_client.get("/api/v1/auth/me", headers=token).status_code == 200
    assert jwks_fetches == before + 1  # key cached; no per-request network


# ------------------------------------------------------------------- dev mode


def test_disabled_mode_is_open_and_loud(client):
    """Default LF_AUTH_MODE=disabled: anonymous admin, everything open."""
    assert client.get("/api/v1/ledgers").status_code == 200
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json() == {"sub": "anonymous", "roles": ["admin"], "auth_mode": "disabled"}


def test_oidc_mode_without_issuer_fails_fast(monkeypatch, sqlalchemy_url):
    from ledgerforge import db
    from ledgerforge.main import create_app
    from ledgerforge.settings import get_settings

    monkeypatch.delenv("LF_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("LF_OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("LF_AUTH_MODE", "oidc")
    get_settings.cache_clear()
    db.reset_engine()
    with pytest.raises(RuntimeError, match="LF_OIDC_ISSUER"):
        create_app()
    get_settings.cache_clear()
    db.reset_engine()
