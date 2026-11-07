"""Cost centres & cost categories (ADR-025, Phase 4) — the first core-schema
change since 0006.

The promises under test:

- **The trees**: cost categories and cost centres are adjacency+closure
  pairs exactly like groups (ADR-001) — trigger-maintained closure, cycle
  and root protection, one root per book, and every real centre answers to
  exactly one category (TallyPrime's rule, a schema constraint).
- **The conservation law**: an entry's allocations sum to the entry's
  amount EXACTLY — a line is fully allocated or carries no allocations at
  all. Enforced by the engine (clean 422) and re-enforced by the database
  at commit (the deferrable trigger, the same discipline as Law 1's
  ``trg_entries_balanced``); Dr/Cr is preserved from the entry, and the
  root centre can never receive an allocation.
- **Reversals mirror** the splits (same centres, same amounts, flipped
  Dr/Cr riding the flipped line) — the centre's contribution nets to zero.
- **The break-up reports**: the centre summary (closure rollup, a parent
  carries its subtree), one centre's ledger-wise detail (own allocations,
  total proven against the rows), and the category rollup (primary
  categories carry their secondaries) — with the Unallocated mass surfaced
  so allocated + unallocated is the window's whole entry mass. FYC sweeps
  are excluded on both sides (the profit_and_loss precedent).
- Books are strangers; idempotent replay allocates nothing twice.

The module shares one database (module-scoped ``fresh_db``); every test
uses its own ledgers and centres so the shared book keeps the tests
independent (the bills suite's discipline).
"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from conftest import post_voucher

# ------------------------------------------------------------------ helpers


def _ensure_ledgers(client, *specs: tuple[str, str], headers: dict | None = None) -> None:
    for name, group in specs:
        r = client.post(
            "/api/v1/ledgers", headers=headers, json={"name": name, "group_name": group}
        )
        assert r.status_code in (201, 422), (name, r.text)


def _make_category(client, name: str, parent: str = "Primary") -> dict:
    r = client.post("/api/v1/cost-categories", json={"name": name, "parent_name": parent})
    if r.status_code == 422 and "already exists" in r.text:
        return next(c for c in client.get("/api/v1/cost-categories").json() if c["name"] == name)
    assert r.status_code == 201, r.text
    return r.json()


def _make_centre(client, name: str, *, parent: str = "Primary", category: str) -> dict:
    r = client.post(
        "/api/v1/cost-centres",
        json={"name": name, "parent_name": parent, "category_name": category},
    )
    if r.status_code == 422 and "already exists" in r.text:
        return next(c for c in client.get("/api/v1/cost-centres").json() if c["name"] == name)
    assert r.status_code == 201, r.text
    return r.json()


def _line(ledger: str, dc: str, amount: str, allocations: list[dict] | None = None) -> dict:
    entry: dict = {"ledger_name": ledger, "dc": dc, "amount": amount}
    if allocations is not None:
        entry["cost_allocations"] = allocations
    return entry


def _split(centre_id: int, amount: str) -> dict:
    return {"centre_id": centre_id, "amount": amount}


def _dsn() -> str:
    """The module's database as a plain psycopg DSN."""
    return os.environ["LF_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def tree(client):
    """A Location category; Mumbai + Pune centres, and Thane under Mumbai."""
    _make_category(client, "Location")
    mumbai = _make_centre(client, "Mumbai", category="Location")
    pune = _make_centre(client, "Pune", category="Location")
    thane = _make_centre(client, "Thane", parent="Mumbai", category="Location")
    return {"location": "Location", "mumbai": mumbai, "pune": pune, "thane": thane}


# ------------------------------------------------------------------ the trees
def test_roots_are_seeded(client):
    """Every book gets a 'Primary' category and a 'Primary' centre — the
    containers the trees hang from (the groups' pattern)."""
    categories = client.get("/api/v1/cost-categories").json()
    centres = client.get("/api/v1/cost-centres").json()
    roots_c = [c for c in categories if c["is_root"]]
    roots_s = [c for c in centres if c["is_root"]]
    assert len(roots_c) == 1 and roots_c[0]["name"] == "Primary"
    assert len(roots_s) == 1 and roots_s[0]["name"] == "Primary"
    # the root centre is the container: no category of its own
    assert roots_s[0]["category_id"] is None


def test_centre_closure_and_guards(client, tree):
    centres = {c["name"]: c for c in client.get("/api/v1/cost-centres").json()}
    assert centres["Thane"]["parent_id"] == centres["Mumbai"]["id"]
    assert centres["Mumbai"]["category_name"] == "Location"

    # duplicate names are refused
    r = client.post(
        "/api/v1/cost-centres",
        json={"name": "Mumbai", "parent_name": "Primary", "category_name": "Location"},
    )
    assert r.status_code == 422

    # re-parenting works and the closure follows (Thane under Pune now)
    r = client.patch(
        f"/api/v1/cost-centres/{centres['Thane']['id']}",
        json={"parent_name": "Pune"},
    )
    assert r.status_code == 200, r.text
    with psycopg.connect(_dsn()) as conn:
        depth = conn.execute(
            "SELECT c.depth FROM cost_centre_closure c"
            " JOIN cost_centres a ON a.id = c.ancestor_id AND a.name = 'Pune'"
            " JOIN cost_centres d ON d.id = c.descendant_id AND d.name = 'Thane'"
        ).fetchone()[0]
    assert depth == 1
    # ...and back
    r = client.patch(
        f"/api/v1/cost-centres/{centres['Thane']['id']}", json={"parent_name": "Mumbai"}
    )
    assert r.status_code == 200

    # a cycle is refused (Mumbai under its own descendant Thane)
    r = client.patch(
        f"/api/v1/cost-centres/{centres['Mumbai']['id']}", json={"parent_name": "Thane"}
    )
    assert r.status_code == 422
    assert "descendant" in r.json()["detail"].lower()

    # the root cannot be re-parented
    root = next(c for c in centres.values() if c["is_root"])
    assert (
        client.patch(
            f"/api/v1/cost-centres/{root['id']}", json={"parent_name": "Mumbai"}
        ).status_code
        == 422
    )

    # categories: the same machinery, and a centre needs one
    r = client.post("/api/v1/cost-centres", json={"name": "Nowhere", "parent_name": "Primary"})
    assert r.status_code == 422  # category_id or category_name is required
    r = client.post(
        "/api/v1/cost-centres",
        json={"name": "Nowhere", "parent_name": "Primary", "category_name": "Missing"},
    )
    assert r.status_code == 404


# ------------------------------------------------------------------ posting
def test_post_with_allocations(client, tree):
    _ensure_ledgers(client, ("Rent", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line(
                "Rent",
                "D",
                "12000.00",
                [_split(tree["mumbai"]["id"], "7000.00"), _split(tree["pune"]["id"], "5000.00")],
            ),
            _line("Cash", "C", "12000.00"),
        ],
    )
    assert r.status_code == 201, r.text
    out = r.json()
    rent_line = out["entries"][0]
    assert rent_line["cost_allocations"] == [
        {
            "centre_id": tree["mumbai"]["id"],
            "centre_name": "Mumbai",
            "dc": "D",
            "amount": "7000.00000000",
        },
        {
            "centre_id": tree["pune"]["id"],
            "centre_name": "Pune",
            "dc": "D",
            "amount": "5000.00000000",
        },
    ]
    # the unallocated line carries an empty list, not null
    assert out["entries"][1]["cost_allocations"] == []


def test_partial_and_over_allocation_refused(client, tree):
    _ensure_ledgers(client, ("Coffee", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    for allocations in (
        [_split(tree["mumbai"]["id"], "100.00")],  # under
        [_split(tree["mumbai"]["id"], "100.00"), _split(tree["pune"]["id"], "200.00")],  # over
    ):
        r = post_voucher(
            client,
            type_="JV",
            entries=[
                _line("Coffee", "D", "250.00", allocations),
                _line("Cash", "C", "250.00"),
            ],
        )
        assert r.status_code == 422, r.text
        assert "fully allocated or left unallocated" in r.json()["detail"]

    # amounts must be positive
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line(
                "Coffee",
                "D",
                "250.00",
                [_split(tree["mumbai"]["id"], "250.00"), _split(tree["pune"]["id"], "0.00")],
            ),
            _line("Cash", "C", "250.00"),
        ],
    )
    assert r.status_code == 422


def test_unknown_or_root_centre_refused(client, tree):
    _ensure_ledgers(client, ("Tea", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    # unknown id
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line("Tea", "D", "10.00", [_split(999999, "10.00")]),
            _line("Cash", "C", "10.00"),
        ],
    )
    assert r.status_code == 404
    # unknown name
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line("Tea", "D", "10.00", [{"centre_name": "Atlantis", "amount": "10.00"}]),
            _line("Cash", "C", "10.00"),
        ],
    )
    assert r.status_code == 404
    # the root is a container, not an allocation target
    roots = [c for c in client.get("/api/v1/cost-centres").json() if c["is_root"]]
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line("Tea", "D", "10.00", [_split(roots[0]["id"], "10.00")]),
            _line("Cash", "C", "10.00"),
        ],
    )
    assert r.status_code == 422
    assert "root" in r.json()["detail"].lower()


def test_database_enforces_the_conservation_law(client, tree):
    """The engine's 422 is the friendly half; the deferred constraint
    trigger is the law itself. A direct SQL insert that under-allocates an
    entry must take the whole transaction down at commit — nothing partial
    ever survives (Law 2's discipline)."""
    from conftest import TODAY

    _ensure_ledgers(client, ("Stationery", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    r = post_voucher(
        client,
        type_="JV",
        entries=[_line("Stationery", "D", "300.00"), _line("Cash", "C", "300.00")],
    )
    assert r.status_code == 201
    voucher_id = r.json()["id"]

    with psycopg.connect(_dsn()) as conn:
        entry_id = conn.execute(
            "SELECT id FROM voucher_entries WHERE voucher_id = %s AND ledger_id ="
            " (SELECT id FROM ledgers WHERE name = 'Stationery')",
            (voucher_id,),
        ).fetchone()[0]
        book_id = conn.execute(
            "SELECT book_id FROM voucher_entries WHERE id = %s", (entry_id,)
        ).fetchone()[0]
        try:
            conn.execute(
                "INSERT INTO cost_allocations (entry_id, centre_id, book_id, dc, amount,"
                " voucher_date) VALUES (%s, %s, %s, 'D', 100.00, %s)",
                (entry_id, tree["mumbai"]["id"], book_id, TODAY),
            )
            conn.commit()
            raise AssertionError("the database accepted a partial allocation")
        except psycopg.errors.RaiseException as exc:
            assert "fully allocated or left unallocated" in str(exc)
            conn.rollback()


def test_reversal_mirrors_allocations(client, tree):
    _ensure_ledgers(client, ("Mirrored", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line("Mirrored", "D", "800.00", [_split(tree["mumbai"]["id"], "800.00")]),
            _line("Cash", "C", "800.00"),
        ],
    )
    assert r.status_code == 201
    voucher_id = r.json()["id"]

    r = client.post(f"/api/v1/vouchers/{voucher_id}/reverse")
    assert r.status_code == 201, r.text
    reversal = r.json()
    # the mirrored line's allocation flipped with it: same centre, Cr now
    assert reversal["entries"][0]["cost_allocations"] == [
        {
            "centre_id": tree["mumbai"]["id"],
            "centre_name": "Mumbai",
            "dc": "C",
            "amount": "800.00000000",
        }
    ]
    # the centre's contribution from this pair nets to exactly zero
    breakup = client.get(
        f"/api/v1/reports/cost-centres/{tree['mumbai']['id']}/breakup?from=2000-01-01&to=2100-01-01"
    ).json()
    mirrored = next((row for row in breakup["ledgers"] if row["ledger_name"] == "Mirrored"), None)
    assert mirrored is not None
    assert Decimal(mirrored["dr"]) - Decimal(mirrored["cr"]) == 0


def test_idempotent_replay_allocates_once(client, tree):
    _ensure_ledgers(client, ("Replayed", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    token = uuid4()
    payload = dict(
        type_="JV",
        entries=[
            _line("Replayed", "D", "60.00", [_split(tree["pune"]["id"], "60.00")]),
            _line("Cash", "C", "60.00"),
        ],
        token=token,
    )
    first = post_voucher(client, **payload)
    assert first.status_code == 201
    second = post_voucher(client, **payload)
    assert second.status_code == 200 and second.json()["replayed"] is True

    with psycopg.connect(_dsn()) as conn:
        count = conn.execute(
            "SELECT count(*) FROM cost_allocations ca"
            " JOIN voucher_entries ve ON ve.id = ca.entry_id"
            " JOIN ledgers l ON l.id = ve.ledger_id"
            " WHERE l.name = 'Replayed'"
        ).fetchone()[0]
    assert count == 1


# ------------------------------------------------------------------ reports
def _py_totals() -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The window's whole entry mass straight from the tables (FYC excluded)
    — allocated (dr, cr) and unallocated (dr, cr) — folded in Python so the
    report's numbers are checked against an independent computation."""
    with psycopg.connect(_dsn()) as conn:
        rows = conn.execute(
            """
            SELECT ve.dc, ve.amount,
                   EXISTS (SELECT 1 FROM cost_allocations ca WHERE ca.entry_id = ve.id)
            FROM voucher_entries ve
            JOIN vouchers v ON v.id = ve.voucher_id
            JOIN voucher_types vt ON vt.id = v.voucher_type_id
            WHERE ve.voucher_date BETWEEN '2000-01-01' AND '2100-01-01'
              AND vt.code <> 'FYC'
            """
        ).fetchall()
    allocated_dr = allocated_cr = unallocated_dr = unallocated_cr = Decimal(0)
    for dc, amount, has_alloc in rows:
        if has_alloc:
            if dc == "D":
                allocated_dr += amount
            else:
                allocated_cr += amount
        else:
            if dc == "D":
                unallocated_dr += amount
            else:
                unallocated_cr += amount
    return allocated_dr, allocated_cr, unallocated_dr, unallocated_cr


def test_summary_breakup_and_category_rollup(client):
    """The trio, on dedicated centres so every figure is deterministic:
    HQ and Andheri (under HQ) in a Branches category."""
    _make_category(client, "Branches")
    hq = _make_centre(client, "HQ", category="Branches")
    andheri = _make_centre(client, "Andheri", parent="HQ", category="Branches")

    _ensure_ledgers(
        client,
        ("Furniture", "Indirect Expenses"),
        ("Salaries", "Indirect Expenses"),
        ("Sales Income", "Sales Accounts"),
        ("Cash", "Cash-in-Hand"),
    )
    r = post_voucher(
        client,
        type_="JV",
        entries=[
            _line(
                "Furniture",
                "D",
                "12000.00",
                [_split(andheri["id"], "7000.00"), _split(hq["id"], "5000.00")],
            ),
            _line("Salaries", "D", "4000.00"),  # deliberately unallocated
            _line("Sales Income", "C", "16000.00", [_split(andheri["id"], "16000.00")]),
        ],
    )
    assert r.status_code == 201, r.text

    summary = client.get("/api/v1/reports/cost-centres?from=2000-01-01&to=2100-01-01").json()
    by_name = {c["centre_name"]: c for c in summary["centres"]}
    # subtree rollup: HQ carries Andheri's movements too
    assert Decimal(by_name["HQ"]["dr"]) == Decimal("12000.00")
    assert Decimal(by_name["HQ"]["cr"]) == Decimal("16000.00")
    assert Decimal(by_name["Andheri"]["dr"]) == Decimal("7000.00")
    assert Decimal(by_name["Andheri"]["cr"]) == Decimal("16000.00")
    # the root carries the book's whole allocated mass — the independent fold
    a_dr, a_cr, u_dr, u_cr = _py_totals()
    root = next(c for c in summary["centres"] if c["is_root"])
    assert Decimal(root["dr"]) == a_dr and Decimal(root["cr"]) == a_cr
    assert Decimal(summary["unallocated"]["dr"]) == u_dr
    assert Decimal(summary["unallocated"]["cr"]) == u_cr

    # the break-up: HQ's OWN ledger-wise detail (no subtree lines)
    breakup = client.get(
        f"/api/v1/reports/cost-centres/{hq['id']}/breakup?from=2000-01-01&to=2100-01-01"
    ).json()
    furniture = next(row for row in breakup["ledgers"] if row["ledger_name"] == "Furniture")
    assert Decimal(furniture["dr"]) == Decimal("5000.00")
    assert Decimal(breakup["totals"]["dr"]) == sum(Decimal(row["dr"]) for row in breakup["ledgers"])
    # Andheri's credit does NOT appear in HQ's own break-up (it is Andheri's)
    assert all(row["ledger_name"] != "Sales Income" for row in breakup["ledgers"])

    # unknown / root centres are 404
    assert (
        client.get(
            "/api/v1/reports/cost-centres/999999/breakup?from=2000-01-01&to=2100-01-01"
        ).status_code
        == 404
    )
    root_id = next(c["centre_id"] for c in summary["centres"] if c["is_root"])
    assert (
        client.get(
            f"/api/v1/reports/cost-centres/{root_id}/breakup?from=2000-01-01&to=2100-01-01"
        ).status_code
        == 404
    )

    # the category rollup: Branches carries its centres' whole mass
    categories = client.get("/api/v1/reports/cost-categories?from=2000-01-01&to=2100-01-01").json()
    branches = next(c for c in categories["categories"] if c["category_name"] == "Branches")
    assert Decimal(branches["dr"]) == Decimal("12000.00")
    assert Decimal(branches["cr"]) == Decimal("16000.00")
    primary = next(c for c in categories["categories"] if c["is_root"])
    assert Decimal(primary["dr"]) == a_dr and Decimal(primary["cr"]) == a_cr
    assert Decimal(categories["unallocated"]["dr"]) == u_dr
    assert Decimal(categories["unallocated"]["cr"]) == u_cr


def test_fyc_sweeps_are_excluded(client, tree):
    """The FYC transfer is not an operating fact — its mass never reaches
    the break-up (neither allocated nor unallocated)."""
    _ensure_ledgers(client, ("FYC Probe", "Indirect Expenses"), ("Cash", "Cash-in-Hand"))
    r = post_voucher(
        client,
        type_="FYC",
        entries=[
            _line("FYC Probe", "D", "777.00"),
            _line("Profit & Loss A/c", "C", "777.00"),
        ],
    )
    assert r.status_code == 201, r.text

    # the report's unallocated must equal the FYC-excluding fold exactly —
    # if the sweep leaked in, the two would differ by 777.00
    summary = client.get("/api/v1/reports/cost-centres?from=2000-01-01&to=2100-01-01").json()
    _a_dr, _a_cr, u_dr, u_cr = _py_totals()
    assert Decimal(summary["unallocated"]["dr"]) == u_dr
    assert Decimal(summary["unallocated"]["cr"]) == u_cr

    with psycopg.connect(_dsn()) as conn:
        probe_rows = conn.execute(
            "SELECT count(*) FROM rpt.cost_centre_summary("
            "(SELECT id FROM books LIMIT 1), '2000-01-01', '2100-01-01')"
            " WHERE out_centre_name = 'Unallocated' AND out_dr = 777"
        ).fetchone()[0]
    assert probe_rows == 0


def test_books_are_strangers(client, tree):
    """Another book's centre ids are 404s, never cross-book allocations;
    its reports never see this book's mass (ADR-009's discipline)."""
    r = client.post("/api/v1/books", json={"name": "Stranger Co", "slug": "stranger"})
    assert r.status_code == 201, r.text
    stranger = r.json()["book"]
    stranger_headers = {"X-Book-Id": str(stranger["id"])}

    _ensure_ledgers(
        client,
        ("Stranger Expense", "Indirect Expenses"),
        ("Cash", "Cash-in-Hand"),
        headers=stranger_headers,
    )
    # this book's Mumbai centre id is unknown in the stranger book
    r = client.post(
        "/api/v1/vouchers",
        headers=stranger_headers,
        json={
            "voucher_type": "JV",
            "voucher_date": "2026-04-05",
            "entries": [
                _line("Stranger Expense", "D", "50.00", [_split(tree["mumbai"]["id"], "50.00")]),
                _line("Cash", "C", "50.00"),
            ],
        },
    )
    assert r.status_code == 404

    # and the stranger book's summary is blind to this book's allocations
    stranger_summary = client.get(
        "/api/v1/reports/cost-centres?from=2000-01-01&to=2100-01-01",
        headers=stranger_headers,
    ).json()
    assert stranger_summary["centres"] == []
    assert Decimal(stranger_summary["unallocated"]["dr"]) == 0

    # archive the stranger so the module's no-header requests keep
    # auto-resolving to the single active book
    r = client.patch(f"/api/v1/books/{stranger['id']}", json={"is_archived": True})
    assert r.status_code == 200, r.text
