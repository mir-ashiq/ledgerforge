"""Report scale-out (ADR-018): the balance cache and the fast read path,
plus the operator-initiated yearly partitioning of ``voucher_entries``.

Two correctness stories run through this module:

- **The cache can only be slow, never wrong.** Every fast report is asserted
  equal to its slow twin across period topologies: no periods, closed
  history, incremental closes, a new open period created below the horizon
  (the invalidation trigger), reopen cycles, all-frozen books, gap and
  pre-period entries, and FYC sweeps. The horizon rule itself is ADR-008's
  posting gate: only OPEN-period dates post, so everything below
  ``min(open starts_on)`` is frozen forever.
- **Partitioning is invisible to the accounting.** Conversion -> reports
  identical; Law 1, immutability, line uniqueness and FKs enforced on the
  partitioned shape; postings into a missing year create its partition
  on demand (the vouchers-side SECURITY DEFINER trigger); unpartition
  restores the plain shape with reports still identical; cache rows are
  shape-agnostic and survive both directions.

Each test gets a full schema reset: partition conversion and cache rows are
global state, and tests must not chain.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest
from conftest import REPO_ROOT, create_ledger, create_period, post_voucher


# ------------------------------------------------------------------ helpers
@pytest.fixture()
def plain_db(psycopg_dsn):
    """Function-scoped full reset: a clean plain-shape database per test."""
    from ledgerforge import bootstrap

    with psycopg.connect(psycopg_dsn, autocommit=True) as conn:
        for schema in ("public", "rpt", "ops"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("CREATE SCHEMA public")
    bootstrap.apply_sql_files(psycopg_dsn, REPO_ROOT, echo=False)
    return psycopg_dsn


def _book_id(client) -> int:
    books = client.get("/api/v1/books").json()
    assert books, "no book — seed missing"
    return books[0]["id"]


def _refresh(dsn, book=None) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT out_book_id, out_ledgers, out_horizon, out_action "
            "FROM rpt.refresh_balance_cache(%s)",
            (book,),
        ).fetchall()
    return rows


def _cache(dsn, book) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT ledger_id, horizon, dr, cr FROM rpt.balance_cache "
            "WHERE book_id = %s ORDER BY ledger_id",
            (book,),
        ).fetchall()


def _convert(dsn, headroom: int = 2) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT out_step, out_detail FROM ops.partition_entries_by_year(%s)",
            (headroom,),
        ).fetchall()


def _unconvert(dsn) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT out_step, out_detail FROM ops.unpartition_entries()").fetchall()


def _relkind(dsn) -> str:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT relkind FROM pg_class WHERE oid = 'public.voucher_entries'::regclass"
        ).fetchone()[0]


def _direct_sums(dsn, book, upto) -> dict[int, tuple[str, str]]:
    """Sums straight from the source of truth (the slow path's own math)."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT e.ledger_id,
                   COALESCE(SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END), 0)
            FROM voucher_entries e
            JOIN vouchers v ON v.id = e.voucher_id
            WHERE e.book_id = %s AND v.voucher_date <= %s
            GROUP BY e.ledger_id
            """,
            (book, upto),
        ).fetchall()
    return {r[0]: (str(r[1]), str(r[2])) for r in rows}


def _sale(client, ledger: str, amount: str, date_: str) -> dict:
    r = post_voucher(
        client,
        type_="SAL",
        date_=date_,
        entries=[
            {"ledger_name": "Cash", "dc": "D", "amount": amount},
            {"ledger_name": ledger, "dc": "C", "amount": amount},
        ],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _close(client, period: dict) -> None:
    assert client.post(f"/api/v1/periods/{period['id']}/lock").status_code == 200
    assert client.post(f"/api/v1/periods/{period['id']}/close").status_code == 200


def _reports(client, fast: bool, pl_from: str, pl_to: str) -> dict:
    flag = "&fast=true" if fast else ""
    tb = client.get(
        "/api/v1/reports/trial-balance?fast=true" if fast else "/api/v1/reports/trial-balance"
    ).json()
    bs = client.get(
        "/api/v1/reports/balance-sheet?fast=true" if fast else "/api/v1/reports/balance-sheet"
    ).json()
    pl = client.get(f"/api/v1/reports/profit-loss?from={pl_from}&to={pl_to}{flag}").json()
    return {"trial_balance": tb, "balance_sheet": bs, "profit_loss": pl}


# ------------------------------------------------------- the balance cache
def test_no_periods_cache_disabled_fast_equals_slow(client, plain_db):
    """A book without periods has no provably frozen zone: the refresh
    disables itself and fast reads fall back — exactly equal, never wrong."""
    create_ledger(client, "Legacy Sales", "Sales Accounts")
    _sale(client, "Legacy Sales", "100.00", "2025-06-01")
    _sale(client, "Legacy Sales", "40.00", "2026-02-02")

    rows = _refresh(plain_db)
    assert rows[0][2] is None  # horizon NULL
    assert "disabled" in rows[0][3]
    assert _cache(plain_db, _book_id(client)) == []

    slow = _reports(client, False, "2025-01-01", "2026-12-31")
    fast = _reports(client, True, "2025-01-01", "2026-12-31")
    assert fast == slow
    assert Decimal(fast["trial_balance"]["totals"]["difference"]) == 0


def test_closed_history_caches_and_fast_equals_slow(client, plain_db):
    """Close history, refresh, and every fast report equals its slow twin —
    including a P&L that spans the horizon and the FYC sweeps."""
    create_ledger(client, "Cached Sales", "Sales Accounts")
    for date_, amount in (
        ("2024-03-01", "100.00"),
        ("2024-11-05", "25.50"),
        ("2025-07-07", "60.00"),
        ("2026-01-15", "13.00"),
        ("2026-09-09", "7.25"),
    ):
        _sale(client, "Cached Sales", amount, date_)

    book = _book_id(client)
    p1 = create_period(client, "S1 FY2024", "2024-01-01", "2024-12-31")
    p2 = create_period(client, "S1 FY2025", "2025-01-01", "2025-12-31")
    create_period(client, "S1 FY2026-27", "2026-01-01", "2027-03-31")
    _close(client, p1)
    _close(client, p2)

    rows = _refresh(plain_db)
    assert rows[0][1] > 0  # cached rows exist
    horizon = rows[0][2]
    assert str(horizon) == "2026-01-01"  # min open starts_on

    # the cached zone holds exactly the direct sums below the horizon
    cache = _cache(plain_db, book)
    direct = _direct_sums(plain_db, book, "2025-12-31")
    assert len(cache) == len(direct)
    for ledger_id, h, dr, cr in cache:
        assert str(h) == "2026-01-01"
        assert (Decimal(dr), Decimal(cr)) == tuple(Decimal(x) for x in direct[ledger_id])

    slow = _reports(client, False, "2024-01-01", "2026-12-31")
    fast = _reports(client, True, "2024-01-01", "2026-12-31")
    assert fast == slow
    assert Decimal(fast["trial_balance"]["totals"]["difference"]) == 0
    assert Decimal(fast["balance_sheet"]["totals"]["difference"]) == 0


def test_cache_incremental_extension(client, plain_db):
    """A second refresh after more history closed extends the same rows by
    the newly frozen delta zone — never a full rebuild from zero."""
    create_ledger(client, "Incr Sales", "Sales Accounts")
    _sale(client, "Incr Sales", "50.00", "2024-05-05")
    p1 = create_period(client, "S2 FY2024", "2024-01-01", "2024-12-31")
    p2 = create_period(client, "S2 FY2025", "2025-01-01", "2025-12-31")
    create_period(client, "S2 FY2026", "2026-01-01", "2026-12-31")
    _close(client, p1)

    book = _book_id(client)
    _refresh(plain_db)
    before = _cache(plain_db, book)
    assert before

    # more activity in the (still open) middle period, then freeze it too
    _sale(client, "Incr Sales", "70.00", "2025-08-08")
    _close(client, p2)
    _refresh(plain_db)
    after = _cache(plain_db, book)

    assert str(after[0][1]) == "2026-01-01"  # horizon moved up with the close
    # rows only ever extend: the previous ledger set is a subset, and ledgers
    # the newly frozen zone now covers (P&L A/c, swept by the FYC close) join
    after_ids = {r[0] for r in after}
    before_ids = {r[0] for r in before}
    assert before_ids <= after_ids
    # the incremental extension: Cash grew by exactly the 2025 delta (70 D)
    delta_by_ledger = {
        r[0]: r[2] - next(b[2] for b in before if b[0] == r[0]) for r in after if r[0] in before_ids
    }
    assert max(delta_by_ledger.values()) == Decimal("70.00000000")

    slow = _reports(client, False, "2024-01-01", "2026-12-31")
    fast = _reports(client, True, "2024-01-01", "2026-12-31")
    assert fast == slow


def test_new_open_period_below_horizon_invalidates(client, plain_db):
    """The one administrative way frozen dates become writable again: a new
    open period below the horizon. The rows die in that same transaction —
    and the refreshed cache absorbs the backdated postings that follow."""
    create_ledger(client, "Inv Sales", "Sales Accounts")
    _sale(client, "Inv Sales", "80.00", "2024-04-04")
    p1 = create_period(client, "S3 FY2024", "2024-01-01", "2024-12-31")
    create_period(client, "S3 FY2025-26", "2025-01-01", "2026-12-31")
    _close(client, p1)

    book = _book_id(client)
    _refresh(plain_db)
    assert _cache(plain_db, book)

    # an open period below the cached horizon: every row that folded those
    # dates in dies NOW, inside the INSERT's transaction
    create_period(client, "S3 FY2023", "2023-01-01", "2023-12-31")
    assert _cache(plain_db, book) == []

    # backdate into the reopened territory, refresh, and fast is exact again
    _sale(client, "Inv Sales", "19.00", "2023-07-07")
    rows = _refresh(plain_db)
    assert str(rows[0][2]) == "2023-01-01"  # horizon = min open starts_on
    slow = _reports(client, False, "2023-01-01", "2026-12-31")
    fast = _reports(client, True, "2023-01-01", "2026-12-31")
    assert fast == slow


def test_reopen_keeps_cache_valid(client, plain_db):
    """Reopening a locked period cannot un-freeze anything below the
    horizon: it was already non-closed, so the horizon never covered it."""
    create_ledger(client, "Re Sales", "Sales Accounts")
    _sale(client, "Re Sales", "30.00", "2024-06-06")
    p1 = create_period(client, "S4 FY2024", "2024-01-01", "2024-12-31")
    p2 = create_period(client, "S4 FY2025", "2025-01-01", "2025-12-31")
    create_period(client, "S4 FY2026", "2026-01-01", "2026-12-31")
    _close(client, p1)

    _refresh(plain_db)
    before = _cache(plain_db, _book_id(client))

    client.post(f"/api/v1/periods/{p2['id']}/lock")
    assert client.post(f"/api/v1/periods/{p2['id']}/unlock").status_code == 200
    assert _cache(plain_db, _book_id(client)) == before  # untouched

    slow = _reports(client, False, "2024-01-01", "2026-12-31")
    fast = _reports(client, True, "2024-01-01", "2026-12-31")
    assert fast == slow


def test_all_frozen_book_horizon_sentinel(client, plain_db):
    """No open period remains (the last one is locked, so nothing can ever
    post again): the horizon is the sentinel date. Point-in-time reads at
    today fall back to scans (the row would overshoot), still exactly."""
    create_ledger(client, "Frozen Sales", "Sales Accounts")
    _sale(client, "Frozen Sales", "44.00", "2024-08-08")
    p1 = create_period(client, "S5 FY2024", "2024-01-01", "2024-12-31")
    p2 = create_period(client, "S5 FY2025", "2025-01-01", "2025-12-31")
    p3 = create_period(client, "S5 FY2026", "2026-01-01", "2026-12-31")
    _close(client, p1)
    _close(client, p2)
    assert client.post(f"/api/v1/periods/{p3['id']}/lock").status_code == 200

    rows = _refresh(plain_db)
    assert str(rows[0][2]) == "9999-12-31"

    slow = _reports(client, False, "2024-01-01", "2024-12-31")
    fast = _reports(client, True, "2024-01-01", "2024-12-31")
    assert fast == slow


def test_gap_and_pre_period_entries_cacheable(client, plain_db):
    """Entries posted before the book adopted periods are dated below every
    period — uncovered, hence refused by the gate forever, hence frozen: the
    cache may fold them in safely. (Gaps BETWEEN periods cannot hold entries:
    the close ceremony requires the next period to cover the sweep date, so
    closed history is contiguous by construction.)"""
    create_ledger(client, "Gap Sales", "Sales Accounts")
    _sale(client, "Gap Sales", "11.00", "2023-06-01")  # pre-period, uncovered
    _sale(client, "Gap Sales", "22.00", "2024-03-01")  # will be covered
    p1 = create_period(client, "S6 FY2024", "2024-01-01", "2024-12-31")
    create_period(client, "S6 FY2025", "2025-01-01", "2025-12-31")
    _close(client, p1)

    book = _book_id(client)
    rows = _refresh(plain_db)
    assert str(rows[0][2]) == "2025-01-01"

    # both the pre-period entry (2023, gap-frozen) and the closed-period
    # entry (2024) live below the horizon and are cached
    cache = _cache(plain_db, book)
    direct = _direct_sums(plain_db, book, "2024-12-31")
    assert len(cache) == len(direct) == 2  # Cash + Gap Sales
    for ledger_id, _h, dr, cr in cache:
        assert (Decimal(dr), Decimal(cr)) == tuple(Decimal(x) for x in direct[ledger_id])

    slow = _reports(client, False, "2023-01-01", "2025-12-31")
    fast = _reports(client, True, "2023-01-01", "2025-12-31")
    assert fast == slow


def test_pnl_fast_excludes_fyc_like_slow(client, plain_db):
    """FYC sweeps post into the OPEN next period, so cached sums never
    contain them; the fast P&L excludes FYC on both S() terms exactly like
    the slow path — for the closed year AND the year after the sweep."""
    create_ledger(client, "Fyc Sales", "Sales Accounts")
    _sale(client, "Fyc Sales", "200.00", "2024-05-01")
    p1 = create_period(client, "S7 FY2024", "2024-01-01", "2024-12-31")
    create_period(client, "S7 FY2025", "2025-01-01", "2025-12-31")
    _close(client, p1)
    _sale(client, "Fyc Sales", "90.00", "2025-04-04")

    _refresh(plain_db)
    for pl_from, pl_to in (("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31")):
        slow = client.get(f"/api/v1/reports/profit-loss?from={pl_from}&to={pl_to}").json()
        fast = client.get(f"/api/v1/reports/profit-loss?from={pl_from}&to={pl_to}&fast=true").json()
        assert fast == slow
    # the swept year still reports its earnings, not the sweep
    fast_2024 = client.get(
        "/api/v1/reports/profit-loss?from=2024-01-01&to=2024-12-31&fast=true"
    ).json()
    assert fast_2024["trading"]["direct_incomes"]
    assert fast_2024["profit_and_loss"]["net_profit"] == "200.00000000"


def test_ops_api_endpoints(client, plain_db):
    """The admin screen: GET /ops/scale-out and the refresh POST."""
    status = client.get("/api/v1/ops/scale-out")
    assert status.status_code == 200
    body = status.json()["status"]
    assert body["entries_shape"] == "plain"
    assert body["partition_count"] == "0"

    r = client.post("/api/v1/ops/scale-out/refresh-balance-cache", json={})
    assert r.status_code == 200
    assert r.json()["refreshed"][0]["action"].startswith("disabled")  # no periods

    # garbage fast value is a 422, not a 500
    assert client.get("/api/v1/reports/trial-balance?fast=maybe").status_code == 422


# ----------------------------------------------------------- the partitions
def _raw_voucher_sql(no: str, date_: str) -> str:
    return (
        "INSERT INTO vouchers (book_id, voucher_type_id, voucher_no, sequence,"
        " voucher_date, client_token)"
        " SELECT book_id, voucher_type_id, %s, 1, %s, gen_random_uuid()"
        " FROM vouchers LIMIT 1"
    )


def test_partition_round_trip_reports_identical(client, plain_db):
    """Convert -> reports identical -> revert -> reports identical. The
    accounting never notices the storage shape."""
    create_ledger(client, "Part Sales", "Sales Accounts")
    for date_, amount in (
        ("2024-02-02", "10.00"),
        ("2025-02-02", "20.00"),
        ("2026-02-02", "30.00"),
    ):
        _sale(client, "Part Sales", amount, date_)

    before = _reports(client, False, "2024-01-01", "2026-12-31")

    steps = _convert(plain_db, headroom=2)
    assert any(step == "done" for step, _ in steps)
    assert _relkind(plain_db) == "p"

    with psycopg.connect(plain_db) as conn:
        years = [
            r[0]
            for r in conn.execute(
                "SELECT regexp_replace(c.relname, '^voucher_entries_', '') "
                "FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid "
                "WHERE i.inhparent = 'public.voucher_entries'::regclass "
                "ORDER BY 1"
            )
        ]
    assert "2024" in years and "2025" in years and "2026" in years

    during = _reports(client, False, "2024-01-01", "2026-12-31")
    assert during == before

    _unconvert(plain_db)
    assert _relkind(plain_db) == "r"
    after = _reports(client, False, "2024-01-01", "2026-12-31")
    assert after == before


def test_law1_immutable_unique_fk_on_partitioned(client, plain_db):
    """Every invariant survives the conversion, enforced by the same
    triggers cloned onto the partitions."""
    create_ledger(client, "Law Sales", "Sales Accounts")
    sale = _sale(client, "Law Sales", "10.00", "2026-03-03")
    _convert(plain_db, headroom=1)

    with psycopg.connect(plain_db) as conn:
        # unbalanced voucher: the deferrable constraint fires at commit
        conn.execute(_raw_voucher_sql("LAWTEST-1", "2026-04-01"), ("LAWTEST-1", "2026-04-01"))
        vid = conn.execute("SELECT id FROM vouchers WHERE voucher_no = 'LAWTEST-1'").fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException, match="[Uu]nbalanced"):
            conn.execute(
                "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
                " dc, amount, voucher_date)"
                " SELECT %s, 1, ledger_id, book_id, 'D', 5, '2026-04-01'"
                " FROM voucher_entries WHERE voucher_id = %s LIMIT 1",
                (vid, sale["id"]),
            )
            conn.execute(
                "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
                " dc, amount, voucher_date)"
                " SELECT %s, 2, ledger_id, book_id, 'C', 3, '2026-04-01'"
                " FROM voucher_entries WHERE voucher_id = %s LIMIT 1",
                (vid, sale["id"]),
            )
            conn.commit()
        conn.rollback()

        # append-only, enforced through the parent-level trigger
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE voucher_entries SET amount = 1")
        conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM voucher_entries WHERE true")
        conn.rollback()

        # line uniqueness (voucher_id, line_no, voucher_date)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
                " dc, amount, voucher_date)"
                " SELECT voucher_id, line_no, ledger_id, book_id, dc, amount, voucher_date"
                " FROM voucher_entries WHERE voucher_id = %s LIMIT 1",
                (sale["id"],),
            )
        conn.rollback()

        # orphans are rejected: the BEFORE date-guard fires first ("Voucher
        # 999999 does not exist"), which is itself an integrity refusal —
        # the FK underneath raises if the guard is ever bypassed
        with pytest.raises(psycopg.errors.RaiseException, match="does not exist"):
            conn.execute(
                "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
                " dc, amount, voucher_date)"
                " SELECT 999999, 1, ledger_id, book_id, 'D', 1, '2026-04-01'"
                " FROM voucher_entries WHERE voucher_id = %s LIMIT 1",
                (sale["id"],),
            )
        conn.rollback()


def test_posting_into_missing_year_creates_partition(client, plain_db):
    """On the partitioned shape, a voucher dated in a year with no partition
    gets one created on demand — by the vouchers-side SECURITY DEFINER
    trigger (headroom 0, so 2024 is genuinely missing)."""
    create_ledger(client, "NewYear Sales", "Sales Accounts")
    _sale(client, "NewYear Sales", "10.00", "2026-05-05")
    create_period(client, "S8 FY2026", "2026-01-01", "2026-12-31")
    _convert(plain_db, headroom=0)

    # a period below every existing entry year: its partition doesn't exist
    create_period(client, "S8 FY2024", "2024-01-01", "2024-12-31")
    sale = _sale(client, "NewYear Sales", "15.00", "2024-10-10")
    assert sale["voucher_no"]

    with psycopg.connect(plain_db) as conn:
        assert conn.execute("SELECT to_regclass('public.voucher_entries_2024')").fetchone()[0]
        n = conn.execute("SELECT count(*) FROM voucher_entries_2024").fetchone()[0]
    assert n == 2

    slow = client.get("/api/v1/reports/trial-balance").json()
    fast = client.get("/api/v1/reports/trial-balance?fast=true").json()
    assert slow["totals"]["dr"] == fast["totals"]["dr"]


def test_partition_pruning(client, plain_db):
    """The point of the exercise: a dated aggregate plan touches exactly one
    yearly partition."""
    create_ledger(client, "Prune Sales", "Sales Accounts")
    for year in (2024, 2025, 2026):
        _sale(client, "Prune Sales", "10.00", f"{year}-06-06")
    _convert(plain_db, headroom=0)

    with psycopg.connect(plain_db) as conn:
        plan = "\n".join(
            r[0]
            for r in conn.execute(
                "EXPLAIN SELECT sum(amount) FROM voucher_entries "
                "WHERE voucher_date >= '2025-01-01' AND voucher_date < '2026-01-01'"
            ).fetchall()
        )
    assert "voucher_entries_2025" in plan
    assert "voucher_entries_2024" not in plan
    assert "voucher_entries_2026" not in plan


def test_conversion_guards(client, plain_db):
    """Refuse loudly, not silently: double conversion, unpartitioning the
    plain shape, headroom bounds."""
    create_ledger(client, "Guard Sales", "Sales Accounts")
    _sale(client, "Guard Sales", "10.00", "2026-01-01")

    with (
        pytest.raises(psycopg.errors.RaiseException, match="not partitioned"),
        psycopg.connect(plain_db) as conn,
    ):
        conn.execute("SELECT * FROM ops.unpartition_entries()")

    # headroom bounds are validated on the plain shape (before the shape
    # guard would fire)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="headroom"),
        psycopg.connect(plain_db) as conn,
    ):
        conn.execute("SELECT * FROM ops.partition_entries_by_year(99)")

    _convert(plain_db, headroom=2)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="not a plain table"),
        psycopg.connect(plain_db) as conn,
    ):
        conn.execute("SELECT * FROM ops.partition_entries_by_year(2)")
    _unconvert(plain_db)  # leave the shared shape plain


def test_app_role_posts_on_partitioned(client, plain_db):
    """The application role keeps posting through the parent: routed inserts
    check the parent's ACL, and the on-demand partition is created by the
    table owner (SECURITY DEFINER), never by the app role."""
    create_ledger(client, "Role Sales", "Sales Accounts")
    _sale(client, "Role Sales", "10.00", "2026-02-02")
    _convert(plain_db, headroom=0)

    with psycopg.connect(plain_db) as conn:
        conn.execute("SET ROLE ledgerforge_app")
        # a fresh year below the data range (no partition, no app DDL rights)
        conn.execute(_raw_voucher_sql("ROLE-1", "2025-06-01"), ("ROLE-1", "2025-06-01"))
        vid = conn.execute("SELECT id FROM vouchers WHERE voucher_no = 'ROLE-1'").fetchone()[0]
        for line_no, dc in ((1, "D"), (2, "C")):
            conn.execute(
                "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id, book_id,"
                " dc, amount, voucher_date)"
                " SELECT %s, %s, ledger_id, book_id, %s, 5, '2025-06-01'"
                " FROM voucher_entries LIMIT 1",
                (vid, line_no, dc),
            )
        conn.commit()
        conn.execute("RESET ROLE")
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'voucher_entries_2025'"
        ).fetchone()[0]
    assert owner == "postgres"  # the definer (table owner), not ledgerforge_app


def test_cache_survives_repartitioning(client, plain_db):
    """Cache rows sum by date, not by table shape: identical rows before,
    on, and after the partitioned shape; fast stays exact throughout."""
    create_ledger(client, "Shape Sales", "Sales Accounts")
    _sale(client, "Shape Sales", "77.00", "2024-07-07")
    _sale(client, "Shape Sales", "33.00", "2026-01-01")
    p1 = create_period(client, "S9 FY2024", "2024-01-01", "2024-12-31")
    create_period(client, "S9 FY2025-26", "2025-01-01", "2026-12-31")
    _close(client, p1)

    book = _book_id(client)
    _refresh(plain_db)
    before = _cache(plain_db, book)

    _convert(plain_db, headroom=1)
    assert _cache(plain_db, book) == before
    fast_on = _reports(client, True, "2024-01-01", "2026-12-31")

    _unconvert(plain_db)
    assert _cache(plain_db, book) == before
    fast_off = _reports(client, True, "2024-01-01", "2026-12-31")

    slow = _reports(client, False, "2024-01-01", "2026-12-31")
    assert fast_on == slow
    assert fast_off == slow


def test_id_sequence_continues_across_shapes(client, plain_db):
    """Ids are preserved by the copy and the sequence picks up past max —
    no collisions after conversion or revert."""
    create_ledger(client, "Seq Sales", "Sales Accounts")
    _sale(client, "Seq Sales", "10.00", "2025-05-05")

    def max_id():
        with psycopg.connect(plain_db) as conn:
            return conn.execute("SELECT max(id) FROM voucher_entries").fetchone()[0]

    top = max_id()
    _convert(plain_db, headroom=1)
    assert max_id() == top
    _sale(client, "Seq Sales", "12.00", "2025-06-06")
    assert max_id() > top

    top2 = max_id()
    _unconvert(plain_db)
    assert max_id() == top2
    _sale(client, "Seq Sales", "13.00", "2025-07-07")
    assert max_id() > top2


def test_cli_scale_out(client, plain_db, sqlalchemy_url, monkeypatch):
    """The operator surface: ledgerforge scale-out status / refresh-cache /
    partition / ensure / unpartition."""
    from ledgerforge.__main__ import main
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("LF_DATABASE_URL", sqlalchemy_url)

    assert main(["scale-out", "status"]) == 0
    assert main(["scale-out", "refresh-cache"]) == 0
    assert main(["scale-out", "partition", "--headroom", "0"]) == 0
    assert _relkind(plain_db) == "p"
    assert main(["scale-out", "ensure", "2030", "2030"]) == 0
    with psycopg.connect(plain_db) as conn:
        assert conn.execute("SELECT to_regclass('public.voucher_entries_2030')").fetchone()[0]
    assert main(["scale-out", "unpartition"]) == 0
    assert _relkind(plain_db) == "r"
