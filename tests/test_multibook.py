"""Multi-book (ADR-009, migration 0004): one database, many companies.

The promises under test:
- ``POST /books`` creates a company AND its chart of accounts in one action.
- The ``X-Book-Id`` header selects the book; with one active book it is
  optional (single-book-era clients unchanged), with several it is demanded.
- Books are strangers: same ledger names, same voucher numbering from 1,
  same period names and ranges, book-local FX rates, client tokens replayed
  per book — and cross-book references are 404s, never leaks.
- The database, not just the engine, refuses cross-book hierarchy and
  entry/voucher book disagreement (composite FK + trigger).
- ``base_currency`` / ``fy_start_month`` freeze once the book has vouchers.
- The ``books`` claim is the per-book permission seam (auth.require_book_access).
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from ledgerforge.auth import Principal, allowed_book_ids, require_book_access

# ------------------------------------------------------------------ helpers


def _hdr(book_id: int) -> dict[str, str]:
    return {"X-Book-Id": str(book_id)}


def _make_book(client, name: str, **overrides) -> dict:
    payload = {"name": name, "base_currency": "INR"}
    payload.update(overrides)
    r = client.post("/api/v1/books", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _post(client, book_id: int, *, entries: list[dict], token=None, type_: str = "SAL"):
    payload = {"voucher_type": type_, "voucher_date": "2026-06-15", "entries": entries}
    if token is not None:
        payload["client_token"] = str(token)
    return client.post("/api/v1/vouchers", json=payload, headers=_hdr(book_id))


def _ensure_ledger(client, book_id: int, name: str, group: str) -> None:
    r = client.post(
        "/api/v1/ledgers",
        json={"name": name, "group_name": group},
        headers=_hdr(book_id),
    )
    assert r.status_code in (201, 422), r.text  # 422 = already exists


def _post_simple(client, book_id: int, amount: str = "10.00", token=None):
    """A balanced Cash↔Sales pair inside ``book_id``."""
    _ensure_ledger(client, book_id, "Sales", "Sales Accounts")
    return _post(
        client,
        book_id,
        token=token,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": amount},
            {"ledger_name": "Sales", "dc": "C", "amount": amount},
        ],
    )


# =========================================================== the default book
def test_bootstrap_seeds_the_default_book(client, fresh_db):
    books = client.get("/api/v1/books").json()
    assert len(books) == 1
    default = books[0]
    assert default["slug"] == "default"
    assert default["base_currency"] == "INR"
    assert default["fy_start_month"] == 4
    assert default["is_archived"] is False

    # The TallyPrime seed came along: root + 28 groups, system ledgers,
    # 10 voucher types — reachable without a header (exactly one book).
    groups = client.get("/api/v1/groups").json()
    assert len(groups) == 29
    assert {"Sales Accounts", "Duties & Taxes", "Cash-in-Hand"} <= {g["name"] for g in groups}
    ledgers = client.get("/api/v1/ledgers").json()
    assert "Cash" in {ledger["name"] for ledger in ledgers}
    assert "Realized Exchange Gain" in {ledger["name"] for ledger in ledgers}


def test_second_book_gets_the_full_seed_and_a_unique_slug(client, fresh_db):
    made = _make_book(client, "Acme Traders")
    assert made["book"]["name"] == "Acme Traders"
    assert made["book"]["slug"] == "acme-traders"
    assert made["seeded_groups"] == 29
    assert made["seeded_ledgers"] == 6
    assert made["seeded_voucher_types"] == 10

    # Same display name twice: refused. Same slug twice: suffixed.
    dup = client.post("/api/v1/books", json={"name": "Acme Traders"})
    assert dup.status_code == 422
    twin = _make_book(client, "Ácme Traders")  # folds to the same slug
    assert twin["book"]["slug"] == "acme-traders-2"
    assert twin["seeded_groups"] == 29  # the seed is per book, not global

    # The unknown base currency is a clean 404 (currencies are global
    # reference data — create the currency first).
    r = client.post("/api/v1/books", json={"name": "Zeta", "base_currency": "XYZ"})
    assert r.status_code == 404


# ======================================================= header resolution
def test_header_optional_with_one_book_demanded_with_several(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    beta = _make_book(client, "Beta Co")["book"]

    # Several active books, no header: 400 naming the choices.
    r = client.get("/api/v1/ledgers")
    assert r.status_code == 400
    assert "X-Book-Id" in r.json()["detail"]
    assert "Beta Co" in r.json()["detail"]

    # The header wins.
    r = client.get("/api/v1/ledgers", headers=_hdr(default["id"]))
    assert r.status_code == 200
    assert "Cash" in {ledger["name"] for ledger in r.json()}

    # Malformed and unknown ids are 400/404 respectively.
    assert client.get("/api/v1/ledgers", headers=_hdr("acme")).status_code == 400
    assert client.get("/api/v1/ledgers", headers=_hdr(999_999)).status_code == 404

    # Archiving down to one active book restores the optional header.
    # (Earlier tests in this module may have left extra books behind.)
    for book in client.get("/api/v1/books").json():
        if book["id"] != default["id"]:
            r = client.patch(f"/api/v1/books/{book['id']}", json={"is_archived": True})
            assert r.status_code == 200 and r.json()["is_archived"] is True
    assert client.get("/api/v1/ledgers").status_code == 200
    # ...but an explicit header may still select an archived book.
    assert client.get("/api/v1/ledgers", headers=_hdr(beta["id"])).status_code == 200


# ============================================================== isolation
def test_same_ledger_name_in_two_books_and_scoped_lists(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Gamma Co")["book"]

    for book_id in (default["id"], second["id"]):
        r = client.post(
            "/api/v1/ledgers",
            json={"name": "Regional Bank", "group_name": "Bank Accounts"},
            headers=_hdr(book_id),
        )
        assert r.status_code == 201, r.text

    # The duplicate within one book is still refused.
    r = client.post(
        "/api/v1/ledgers",
        json={"name": "Regional Bank", "group_name": "Bank Accounts"},
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 422
    assert "does not exist in book" not in r.json()["detail"]  # a dup, not a miss

    names_default = {
        ledger["name"]
        for ledger in client.get("/api/v1/ledgers", headers=_hdr(default["id"])).json()
    }
    names_second = {
        ledger["name"]
        for ledger in client.get("/api/v1/ledgers", headers=_hdr(second["id"])).json()
    }
    assert "Regional Bank" in names_default and "Regional Bank" in names_second
    assert len(names_default) == len(names_second) == 7  # 6 system + 1 created above


def test_numbering_restarts_per_book(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Delta Co")["book"]

    r1 = _post_simple(client, default["id"])
    r2 = _post_simple(client, second["id"])
    assert r1.status_code == r2.status_code == 201, (r1.text, r2.text)
    # Both books mint their own series from 1 — same voucher_no, different books.
    assert r1.json()["voucher_no"] == r2.json()["voucher_no"]
    assert r1.json()["voucher_no"].startswith("SAL-")
    seq1 = r1.json()["voucher_no"].rsplit("-", 1)[-1]
    assert seq1 == "00001"

    # A second voucher in the default book numbers on; the other book is
    # not affected by it.
    r3 = _post_simple(client, default["id"])
    assert r3.json()["voucher_no"].rsplit("-", 1)[-1] == "00002"


def test_client_token_is_book_scoped(client, fresh_db):
    from uuid import uuid4

    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Epsilon Co")["book"]
    token = uuid4()

    first = _post_simple(client, default["id"], token=token)
    assert first.status_code == 201
    # The same offline retry landing in the OTHER book is a new voucher,
    # not a replay (ADR-009: idempotency resolves within the book).
    other = _post_simple(client, second["id"], token=token)
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]
    # Within the same book it is the original, replayed (200).
    replay = _post_simple(client, default["id"], token=token)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_reports_are_book_scoped(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Zeta Co")["book"]

    # Earlier module tests may have posted into the default book; what this
    # test proves is that Zeta's voucher moves ONLY Zeta's totals.
    before_default = Decimal(
        client.get("/api/v1/reports/trial-balance", headers=_hdr(default["id"])).json()["totals"][
            "dr"
        ]
    )

    r = _post_simple(client, second["id"], "1234.50")
    assert r.status_code == 201, r.text

    after_default = Decimal(
        client.get("/api/v1/reports/trial-balance", headers=_hdr(default["id"])).json()["totals"][
            "dr"
        ]
    )
    tb_second = client.get("/api/v1/reports/trial-balance", headers=_hdr(second["id"])).json()
    assert after_default == before_default  # Zeta's posting is invisible here
    assert Decimal(tb_second["totals"]["dr"]) == Decimal("1234.50")


def test_cross_book_references_are_404s(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Eta Co")["book"]

    posted = _post_simple(client, second["id"]).json()
    # The voucher exists — but not in the default book.
    r = client.get(f"/api/v1/vouchers/{posted['id']}", headers=_hdr(default["id"]))
    assert r.status_code == 404
    assert "does not exist in book" in r.json()["detail"]
    # Through its own book it is found.
    assert (
        client.get(f"/api/v1/vouchers/{posted['id']}", headers=_hdr(second["id"])).status_code
        == 200
    )

    # Posting with the other book's ledger id: a 404, never a cross-book hit.
    second_ledgers = client.get("/api/v1/ledgers", headers=_hdr(second["id"])).json()
    cash_second = next(ledger["id"] for ledger in second_ledgers if ledger["name"] == "Cash")
    r = _post(
        client,
        default["id"],
        entries=[
            {"ledger_id": cash_second, "dc": "D", "amount": "5.00"},
            {"ledger_name": "Sales Accounts", "dc": "C", "amount": "5.00"},
        ],
    )
    assert r.status_code == 404
    assert "does not exist in book" in r.json()["detail"]

    # Reversal across books is refused the same way.
    r = client.post(
        f"/api/v1/vouchers/{posted['id']}/reverse",
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 404


def test_periods_are_per_book(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Theta Co")["book"]

    # Identical name AND range in two books: fine (the global EXCLUDE is
    # gone; per-book non-overlap is the trigger's job).
    for book_id in (default["id"], second["id"]):
        r = client.post(
            "/api/v1/periods",
            json={"name": "FY 2026-27", "starts_on": "2026-04-01", "ends_on": "2027-03-31"},
            headers=_hdr(book_id),
        )
        assert r.status_code == 201, r.text

    # Within one book, overlap is still refused.
    r = client.post(
        "/api/v1/periods",
        json={"name": "Overlap", "starts_on": "2027-01-01", "ends_on": "2027-06-30"},
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 422
    assert "overlaps" in r.json()["detail"].lower()

    # The posting gate is per book: with periods defined in the default
    # book only, Theta posts any date; the default book needs an open period.
    r = _post_simple(client, second["id"])
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/v1/vouchers",
        json={
            "voucher_type": "SAL",
            "voucher_date": "2025-08-08",  # past, but no period covers it in the default book
            "entries": [
                {"ledger_name": "Cash", "dc": "D", "amount": "1.00"},
                {"ledger_name": "Sales", "dc": "C", "amount": "1.00"},
            ],
        },
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 422
    assert "No period covers" in r.json()["detail"]


def test_fx_rates_are_book_local(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Iota Co")["book"]

    r = client.post(
        "/api/v1/currencies", json={"code": "USD", "name": "US Dollar", "minor_unit": 2}
    )
    assert r.status_code == 201, r.text

    r = client.post(
        "/api/v1/fx-rates",
        json={"from_ccy": "USD", "to_ccy": "INR", "rate": "83.50", "valid_on": "2026-06-01"},
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 201, r.text

    # The other book has no rate yet: resolve is a 404 there, a row here.
    r = client.get(
        "/api/v1/fx-rates/resolve",
        params={"from_ccy": "USD", "to_ccy": "INR", "on": "2026-06-15"},
        headers=_hdr(second["id"]),
    )
    assert r.status_code == 404
    r = client.get(
        "/api/v1/fx-rates/resolve",
        params={"from_ccy": "USD", "to_ccy": "INR", "on": "2026-06-15"},
        headers=_hdr(default["id"]),
    )
    assert r.status_code == 200 and Decimal(r.json()["rate"]) == Decimal("83.50")


# ==================================================== mutability and guards
def test_base_currency_and_fy_freeze_once_vouchers_exist(client, fresh_db):
    default = client.get("/api/v1/books").json()[0]
    fresh = _make_book(client, "Kappa Co")["book"]

    # Before any voucher: both are patchable (setup-time corrections).
    r = client.patch(f"/api/v1/books/{fresh['id']}", json={"fy_start_month": 1})
    assert r.status_code == 200 and r.json()["fy_start_month"] == 1
    r = client.patch(f"/api/v1/books/{fresh['id']}", json={"name": "Kappa Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Kappa Renamed"

    # The USD currency must exist for the pre-voucher switch below.
    client.post("/api/v1/currencies", json={"code": "USD", "name": "US Dollar"})
    r = client.patch(f"/api/v1/books/{fresh['id']}", json={"base_currency": "USD"})
    assert r.status_code == 200 and r.json()["base_currency"] == "USD"
    # ...and back (still no vouchers).
    r = client.patch(f"/api/v1/books/{fresh['id']}", json={"base_currency": "INR"})
    assert r.status_code == 200

    # Once posted, both are history: Law 2 forbids restating it.
    assert _post_simple(client, default["id"]).status_code == 201
    r = client.patch(f"/api/v1/books/{default['id']}", json={"base_currency": "USD"})
    assert r.status_code == 409
    r = client.patch(f"/api/v1/books/{default['id']}", json={"fy_start_month": 1})
    assert r.status_code == 409
    # Renaming stays allowed — a label, not history.
    r = client.patch(f"/api/v1/books/{default['id']}", json={"name": "My Company Ltd"})
    assert r.status_code == 200


def test_books_claim_gates_access(client, fresh_db):
    """The enforcement seam (ADR-009): the books claim decides, admin
    bypasses, absent claims mean the single-book-era contract."""
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Lambda Co")["book"]

    def principal(claims: dict) -> Principal:
        return Principal(sub="p", roles=("reader",), claims=claims)

    # no claim → every book (the pre-0004 contract)
    assert allowed_book_ids(principal({})) is None
    # ["*"] → every book
    assert allowed_book_ids(principal({"books": ["*"]})) is None
    # a list → exactly those books
    assert allowed_book_ids(principal({"books": [default["id"]]})) == {default["id"]}
    # junk entries fail closed: nothing is allowed
    assert allowed_book_ids(principal({"books": ["nope", 7.5]})) == set()
    # admin ignores the claim entirely
    admin = Principal(sub="a", roles=("admin",), claims={"books": [second["id"]]})
    assert allowed_book_ids(admin) is None
    # malformed claim shape fails closed
    assert allowed_book_ids(principal({"books": "oddball"})) == set()

    # the seam raises 403 for a book outside the claim
    require_book_access(principal({"books": [default["id"]]}), default["id"])  # inside: fine
    with pytest.raises(HTTPException) as exc:
        require_book_access(principal({"books": [default["id"]]}), second["id"])
    assert exc.value.status_code == 403
    require_book_access(admin, second["id"])  # admin: always fine


# ================================================ database-level defenses
def test_db_rejects_cross_book_hierarchy_and_entry_book(client, fresh_db, psycopg_dsn):
    """The schema itself is the guard: a group cannot parent into another
    book (composite FK), and an entry's book must equal its voucher's
    (trigger) — no API required."""
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Mu Co")["book"]

    with psycopg.connect(psycopg_dsn) as conn:
        (root_default,) = conn.execute(
            "SELECT id FROM groups WHERE book_id = %s AND is_root", (default["id"],)
        ).fetchone()
        (root_second,) = conn.execute(
            "SELECT id FROM groups WHERE book_id = %s AND is_root", (second["id"],)
        ).fetchone()

        # Cross-book parenting: the composite FK (book_id, parent_id) refuses.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO groups (book_id, name, parent_id, nature)
                VALUES (%s, 'Sneaky Sub', %s, 'asset')
                """,
                (second["id"], root_default),
            )
        conn.rollback()

        # Entry book disagreement: the trigger refuses (voucher in book A,
        # entry claiming book B).
        conn.execute("BEGIN")
        (vtype,) = conn.execute(
            "SELECT id FROM voucher_types WHERE book_id = %s AND code = 'JV'",
            (default["id"],),
        ).fetchone()
        (cash,) = conn.execute(
            "SELECT id FROM ledgers WHERE book_id = %s AND name = 'Cash'", (default["id"],)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO vouchers (book_id, client_token, voucher_type_id,
                                  voucher_no, sequence, voucher_date)
            VALUES (%s, gen_random_uuid(), %s, 'JV-XBOOK-1', 1, CURRENT_DATE)
            """,
            (default["id"], vtype),
        )
        (vid,) = conn.execute("SELECT id FROM vouchers WHERE voucher_no = 'JV-XBOOK-1'").fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="does not match voucher book"):
            conn.execute(
                """
                INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,
                                             dc, amount, voucher_date)
                VALUES (%s, 1, %s, %s, 'D', 1, CURRENT_DATE)
                """,
                (vid, cash, second["id"]),
            )
        conn.rollback()


def test_two_books_on_the_raw_report_functions(client, fresh_db, db_session):
    """The rpt.* functions are book-scoped by their first parameter — the
    README's raw-SQL form. Two books, one voucher each: each function
    counts only its own book's facts."""
    default = client.get("/api/v1/books").json()[0]
    second = _make_book(client, "Nu Co")["book"]

    def cash_dr(book_id: int) -> Decimal:
        row = db_session.execute(
            text(
                "SELECT out_dr FROM rpt.trial_balance(:book, :as_of) WHERE out_ledger_name = 'Cash'"
            ),
            {"book": book_id, "as_of": "2026-12-31"},
        ).first()
        return Decimal(row[0]) if row else Decimal("0.00")  # no activity yet

    before_default = cash_dr(default["id"])
    before_second = cash_dr(second["id"])

    assert _post_simple(client, default["id"], "100.00").status_code == 201
    assert _post_simple(client, second["id"], "200.00").status_code == 201

    assert cash_dr(default["id"]) == before_default + Decimal("100.00")
    assert cash_dr(second["id"]) == before_second + Decimal("200.00")
    db_session.rollback()
