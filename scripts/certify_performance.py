#!/usr/bin/env python3
"""Performance certification at 10x (ADR-032): one script, one exit code.

Seeds the 10x book against a real PostgreSQL — 3,000 groups, 34,000
ledgers, 1.2M voucher entries spread over three financial years, the
scale-out partitions engaged — then certifies two budgets and prints the
certificate:

- **Posting**: sustained throughput through the engine's public posting
  path (the same code as the HTTP endpoint) of at least 2,000 vouchers
  per minute, concurrent, with gapless numbering and an exactly-balanced
  trial balance at the end.
- **Reports**: the warm cached trial balance AND the profit & loss on the
  10x book answer in under one second each, through the ADR-018
  partitioned + cached paths, which the script first proves equal the
  slow path to the cent.

Runs in CI as the `certify` job (pushes to main and v* tags) and in the
release workflow's publish job — the tag being released is the tag that
certified. `--smoke` seeds a small book for a quick harness check (the
budgets still assert, at proportionally tiny scale).

Reproducible: `python scripts/certify_performance.py`.
"""

from __future__ import annotations

import os
import pathlib
import random
import threading
import time
import urllib.parse
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg

# the 10x workload model (analysis.md §2.3 x10)
N_GROUPS = 3_000
N_LEDGERS = 34_000
N_VOUCHERS = 600_000  # x 2 entries = 1.2M voucher entries
YEARS = (2024, 2025, 2026)
CLOSED_YEARS = (2024, 2025)  # the balance cache's habitat
LEDGERS_PER_GROUP = N_LEDGERS // (N_GROUPS // 30)  # ~340 under each leaf parent

# budget 1 — posting: vouchers through the public path, concurrent
POST_THREADS = 8
POST_PER_THREAD = 300
POST_TOTAL = POST_THREADS * POST_PER_THREAD  # 2,400
POST_BUDGET_PER_MINUTE = 2_000  # the certified floor

# budget 2 — reports: warm TB and P&L under a second
REPORT_BUDGET_SECONDS = 1.0

DBNAME = "lf_certify"


def _rebuild(db_uri: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(db_uri)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


def _as_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed(dsn: str, smoke: bool) -> tuple[int, int, int]:
    """The 10x book: a wide deep chart, 34k ledgers, 1.2M entries across
    three financial years — direct SQL (COPY), because this certifies
    READS and the posting path, not the seeding."""
    n_groups = 120 if smoke else N_GROUPS
    n_ledgers = 800 if smoke else N_LEDGERS
    n_vouchers = 3_000 if smoke else N_VOUCHERS

    with psycopg.connect(dsn) as conn:
        book = conn.execute("SELECT id FROM books LIMIT 1").fetchone()[0]
        vt = conn.execute("SELECT id FROM voucher_types WHERE code = 'SAL'").fetchone()[0]
        root = conn.execute("SELECT id FROM groups WHERE parent_id IS NULL LIMIT 1").fetchone()[0]

        # the chart: 100 parents x 30 leaves (the closure table fills in)
        print(
            f"seeding the 10x book: {n_groups} groups, {n_ledgers} ledgers,"
            f" {n_vouchers} vouchers ({n_vouchers * 2:,} entries) ..."
        )
        started = time.perf_counter()

        parents: list[int] = []
        with conn.cursor() as cur:
            for p in range(n_groups // 30):
                cur.execute(
                    "INSERT INTO groups (name, parent_id, book_id, nature)"
                    " VALUES (%s, %s, %s, 'liability') RETURNING id",
                    (f"Cert Parent {p:04d}", root, book),
                )
                parents.append(cur.fetchone()[0])
        leaves: list[int] = []
        with conn.cursor() as cur:
            for parent in parents:
                for leaf in range(30):
                    cur.execute(
                        "INSERT INTO groups (name, parent_id, book_id, nature)"
                        " VALUES (%s, %s, %s, 'liability') RETURNING id",
                        (f"Cert Leaf {parent % 1000:04d}-{leaf:02d}", parent, book),
                    )
                    leaves.append(cur.fetchone()[0])

        # ledgers spread under the leaves (parties and a sales side each)
        ledger_ids: list[int] = []
        with conn.cursor() as cur:
            for i in range(n_ledgers):
                leaf = leaves[i % len(leaves)]
                cur.execute(
                    "INSERT INTO ledgers (name, group_id, book_id)"
                    " VALUES (%s, %s, %s) RETURNING id",
                    (f"Cert Ledger {i:06d}", leaf, book),
                )
                ledger_ids.append(cur.fetchone()[0])
        # the posting budget's counter pair: cash + one big sales ledger
        cash_id = conn.execute(
            "SELECT id FROM ledgers WHERE name = 'Cash' AND book_id = %s", (book,)
        ).fetchone()[0]
        sales_id = ledger_ids[-1]

        # periods: close the history, leave the current year open
        for year in YEARS:
            conn.execute(
                "INSERT INTO periods (name, starts_on, ends_on, book_id) VALUES (%s, %s, %s, %s)",
                (f"Certify FY{year}", date(year, 1, 1), date(year, 12, 31), book),
            )

        # vouchers + entries via COPY: 1.2M rows land in seconds.
        # sequences are per-FY (the engine numbers each year from 1 —
        # ADR-002, and the Indian FY straddles calendar years), so the
        # seed counts by the engine's own fiscal_year_label and the
        # posting budget's counter continues the current year's series.
        from ledgerforge.engine.posting import fiscal_year_label

        rng = random.Random(20261109)
        fy_seq: dict[str, int] = {}
        with conn.cursor().copy(
            "COPY vouchers (book_id, voucher_type_id, voucher_no, sequence,"
            " voucher_date, client_token) FROM STDIN"
        ) as cp:
            for i in range(n_vouchers):
                year = YEARS[i % len(YEARS)]
                vdate = date(year, 1 + (i % 12), 1 + (i % 28))
                label = fiscal_year_label(vdate, 4)
                fy_seq[label] = fy_seq.get(label, 0) + 1
                cp.write_row(
                    (
                        book,
                        vt,
                        f"CERT-{label}-{fy_seq[label]:07d}",
                        fy_seq[label],
                        vdate,
                        uuid4(),
                    )
                )
        # GENERATED ALWAYS ids: this fresh database's vouchers run 1..n
        # (nothing else inserted any) — assert it, then key entries by it
        max_id, count_ids = conn.execute("SELECT max(id), count(*) FROM vouchers").fetchone()
        assert max_id == count_ids == n_vouchers, "seed ids must be 1..n"

        with conn.cursor().copy(
            "COPY voucher_entries (voucher_id, line_no, ledger_id, book_id,"
            " dc, amount, voucher_date) FROM STDIN"
        ) as cp:
            for i in range(n_vouchers):
                year = YEARS[i % len(YEARS)]
                vdate = date(year, 1 + (i % 12), 1 + (i % 28))
                amount = Decimal(f"{(i % 900) + 1}.25")
                ledger = ledger_ids[rng.randrange(n_ledgers)]
                cp.write_row((i + 1, 1, ledger, book, "D", amount, vdate))
                cp.write_row((i + 1, 2, cash_id, book, "C", amount, vdate))

        for year in CLOSED_YEARS:
            conn.execute(
                "UPDATE periods SET status = 'locked', locked_at = now()"
                " WHERE book_id = %s AND name = %s",
                (book, f"Certify FY{year}"),
            )
            conn.execute(
                "UPDATE periods SET status = 'closed', closed_at = now(),"
                " closed_by = 'certify' WHERE book_id = %s AND name = %s",
                (book, f"Certify FY{year}"),
            )

        # hand the current FY's counter to the engine: the posting budget
        # continues the series the seed laid down, per ADR-002's scope —
        # the FY's own max(sequence), read from the table (the seed's
        # calendar-year loop straddles FY boundaries on purpose)
        fy_label = fiscal_year_label(date.today(), 4)
        fy_starts = (
            date(date.today().year, 4, 1)
            if date.today().month >= 4
            else date(date.today().year - 1, 4, 1)
        )
        fy_max = conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM vouchers"
            " WHERE book_id = %s AND voucher_type_id = %s AND voucher_date >= %s",
            (book, vt, fy_starts),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO doc_counters (scope_key, last_value)"
            " VALUES (%s, %s) ON CONFLICT (scope_key)"
            " DO UPDATE SET last_value = EXCLUDED.last_value",
            (f"{book}:{vt}:{fy_label}", fy_max),
        )
        conn.commit()
        print(f"seeded in {time.perf_counter() - started:.1f}s")
        return book, cash_id, sales_id  # type: ignore[return-value]


def _post(cert_dsn: str, book: int, cash_id: int, sales_id: int) -> float:
    """Budget 1: POST_PER_THREAD x POST_THREADS vouchers through the
    engine's public posting path, concurrent; returns elapsed seconds."""
    os.environ["LF_DATABASE_URL"] = cert_dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    from ledgerforge import db
    from ledgerforge.contrib import example_gst
    from ledgerforge.engine.posting import post_voucher
    from ledgerforge.models import Book
    from ledgerforge.plugins import PluginManager
    from ledgerforge.schemas import VoucherCreate, VoucherLineIn
    from ledgerforge.settings import get_settings

    get_settings.cache_clear()
    db.reset_engine()

    session = db.session_factory()()
    book_obj = session.get(Book, book)
    session.close()

    manager = PluginManager()
    manager.load_module(example_gst)
    settings = get_settings()

    results: list[int] = []
    errors: list = []

    def worker(offset: int) -> None:
        local = db.session_factory()()
        try:
            for j in range(POST_PER_THREAD):
                payload = VoucherCreate(
                    voucher_type="SAL",
                    voucher_date=date.today(),
                    client_token=uuid4(),
                    entries=[
                        VoucherLineIn(ledger_id=cash_id, dc="D", amount=f"{j + 1}.25"),
                        VoucherLineIn(ledger_id=sales_id, dc="C", amount=f"{j + 1}.25"),
                    ],
                )
                voucher, _ = post_voucher(local, payload, manager, settings, book_obj)
                results.append(voucher.sequence)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            local.close()

    threads = [
        threading.Thread(target=worker, args=(t * POST_PER_THREAD,)) for t in range(POST_THREADS)
    ]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    assert not errors, f"posting errors: {errors!r}"
    assert len(results) == POST_TOTAL
    return elapsed


def _timed(dsn: str, sql: str, params: tuple, repeats: int = 3) -> tuple[float, list]:
    """Best-of-N wall time and the last result set."""
    best: float | None = None
    rows: list = []
    with psycopg.connect(dsn) as conn:
        for _ in range(repeats):
            start = time.perf_counter()
            rows = conn.execute(sql, params).fetchall()
            elapsed = time.perf_counter() - start
            best = elapsed if best is None else min(best, elapsed)
    assert best is not None
    return best, rows


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LedgerForge 10x performance certification")
    parser.add_argument(
        "--pgserver-dir",
        default="",
        help="boot an embedded PostgreSQL in this directory (requires the pgserver package)",
    )
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("LF_BENCH_ADMIN_URL")
        or _as_psycopg(
            os.environ.get(
                "LF_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
            )
        ),
        help=f"admin DSN of a PostgreSQL server (a fresh {DBNAME} DB is created)",
    )
    parser.add_argument(
        "--smoke", action="store_true", help="tiny book for the harness check (CI PRs)"
    )
    args = parser.parse_args()
    if args.pgserver_dir:
        import pgserver

        args.admin_dsn = pgserver.get_server(args.pgserver_dir).get_uri()

    admin = _as_psycopg(args.admin_dsn)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DBNAME}")
    dsn = _rebuild(admin, DBNAME)

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    from ledgerforge import bootstrap

    bootstrap.apply_sql_files(dsn, repo_root, echo=False)

    book, cash_id, sales_id = _seed(dsn, args.smoke)

    # the scale-out shape: partitions engaged, then the cache
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT * FROM ops.partition_entries_by_year(%s)", (book,))
        conn.commit()
    with psycopg.connect(dsn, autocommit=True) as vac:
        vac.execute("VACUUM ANALYZE voucher_entries")
        vac.execute("VACUUM ANALYZE vouchers")

    # ------------------------------------------------ budget 1: posting
    elapsed = _post(dsn, book, cash_id, sales_id)
    per_minute = POST_TOTAL / elapsed * 60

    with psycopg.connect(dsn) as conn:
        vt = conn.execute("SELECT id FROM voucher_types WHERE code = 'SAL'").fetchone()[0]
        fy_starts = (
            date(date.today().year, 4, 1)
            if date.today().month >= 4
            else date(date.today().year - 1, 4, 1)
        )
        gapless = conn.execute(
            "SELECT min(sequence) = 1 AND max(sequence) = count(*)"
            " AND count(DISTINCT sequence) = count(*)"
            " FROM vouchers WHERE book_id = %s AND voucher_type_id = %s"
            " AND voucher_date >= %s",
            (book, vt, fy_starts),
        ).fetchone()[0]
    assert gapless, "the numbering must stay gapless under concurrency"

    # the correctness proof of the posting budget: the exact trial balance
    from ledgerforge import db
    from ledgerforge.reporting.reports import trial_balance as py_trial_balance

    session = db.session_factory()()
    report = py_trial_balance(session, book, date(2026, 12, 31))
    session.close()
    difference = Decimal(report["totals"]["difference"])
    assert difference == 0, f"the 10x trial balance is off by {difference}"

    # ------------------------------------------------ budget 2: reports
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT * FROM rpt.refresh_balance_cache(%s)", (book,))
        conn.commit()

    tb_sql = "SELECT * FROM rpt.trial_balance(%s, %s, %s)"
    pl_sql = "SELECT * FROM rpt.profit_and_loss(%s, %s, %s, %s)"
    as_of = date(2026, 12, 31)

    with psycopg.connect(dsn) as check:
        tb_slow_rows = sorted(
            check.execute(tb_sql, (book, as_of, False)).fetchall(),
            key=lambda r: r[0] if isinstance(r[0], int) else str(r),
        )
        pl_slow_rows = sorted(
            check.execute(pl_sql, (book, date(2020, 1, 1), as_of, False)).fetchall(),
            key=lambda r: (str(r[0]), str(r[1])),
        )
        tb_fast_rows = sorted(
            check.execute(tb_sql, (book, as_of, True)).fetchall(),
            key=lambda r: r[0] if isinstance(r[0], int) else str(r),
        )
        pl_fast_rows = sorted(
            check.execute(pl_sql, (book, date(2020, 1, 1), as_of, True)).fetchall(),
            key=lambda r: (str(r[0]), str(r[1])),
        )
        assert [str(r) for r in tb_slow_rows] == [str(r) for r in tb_fast_rows], (
            "the cached trial balance must equal the slow path to the cent"
        )
        assert [str(r) for r in pl_slow_rows] == [str(r) for r in pl_fast_rows], (
            "the cached profit & loss must equal the slow path to the cent"
        )

    tb_warm, _rows = _timed(dsn, tb_sql, (book, as_of, True))
    pl_warm, _rows = _timed(dsn, pl_sql, (book, date(2020, 1, 1), as_of, True))

    # ------------------------------------------------ the certificate
    posting_ok = per_minute >= POST_BUDGET_PER_MINUTE
    reports_ok = tb_warm < REPORT_BUDGET_SECONDS and pl_warm < REPORT_BUDGET_SECONDS

    n_entries = 0
    with psycopg.connect(dsn) as conn:
        n_entries = conn.execute("SELECT count(*) FROM voucher_entries").fetchone()[0]

    print()
    print("=" * 68)
    print("LEDGERFORGE PERFORMANCE CERTIFICATE (ADR-032) — the 10x book")
    print("=" * 68)
    print(f"book: {n_entries:,} voucher entries, 3 financial years,")
    print("      partitions engaged, balance cache warm")
    print()
    print(f"posting  : {POST_TOTAL} vouchers, {POST_THREADS} concurrent threads")
    print("           through the engine's public posting path")
    print(
        f"           {per_minute:,.0f} vouchers/minute"
        f"  (budget: >= {POST_BUDGET_PER_MINUTE:,})  "
        f"{'PASS' if posting_ok else 'FAIL'}"
    )
    print("           numbering gapless, trial balance difference exactly 0")
    print()
    print(
        f"reports  : warm trial balance   {tb_warm * 1000:8.1f} ms"
        f"  (budget: < {REPORT_BUDGET_SECONDS * 1000:.0f} ms)"
    )
    print(
        f"           warm profit & loss   {pl_warm * 1000:8.1f} ms"
        f"  (budget: < {REPORT_BUDGET_SECONDS * 1000:.0f} ms)"
        f"  {'PASS' if reports_ok else 'FAIL'}"
    )
    print("           both equal the slow path to the cent")
    print("=" * 68)

    if not (posting_ok and reports_ok):
        raise SystemExit(1)
    print("CERTIFIED — both budgets hold at 10x the workload model")


if __name__ == "__main__":
    main()
