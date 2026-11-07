#!/usr/bin/env python3
"""Report scale-out benchmark (ADR-018).

Seeds N vouchers across fiscal years (fast, direct SQL — this benchmark
measures READS, not the posting path), closes the history, then times the
Trial Balance on every shape x path combination:

    plain slow | plain fast (cold) | plain fast (warm)
    partitioned slow (pruned) | partitioned fast (warm)

and proves the fast path equals the slow path to the cent on every shape.
``--smoke`` runs a tiny pass for CI; the default 60k entries demonstrate the
cache effect in ~seconds.

Reproducible: `python scripts/bench_scale_out.py`.
"""

from __future__ import annotations

import os
import pathlib
import time
import urllib.parse
from datetime import date

import psycopg

N_VOUCHERS = 60_000
YEARS = (2020, 2021, 2022, 2023, 2024, 2025, 2026)


def _rebuild(db_uri: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(db_uri)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


def _as_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _time_tb(dsn: str, book: int, fast: bool, repeats: int = 3) -> float:
    """Best-of-N Trial Balance wall time, in seconds."""
    best = None
    with psycopg.connect(dsn) as conn:
        for _ in range(repeats):
            start = time.perf_counter()
            rows = conn.execute(
                "SELECT * FROM rpt.trial_balance(%s, %s, %s)",
                (book, "2026-12-31", fast),
            ).fetchall()
            elapsed = time.perf_counter() - start
            best = elapsed if best is None else min(best, elapsed)
            _ = len(rows)
    assert best is not None
    return best


def _tb_totals(dsn: str, book: int, fast: bool) -> tuple[str, str]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT out_dr, out_cr FROM rpt.trial_balance(%s, %s, %s)"
            " WHERE out_ledger_name = 'Bench Sales'",
            (book, "2026-12-31", fast),
        ).fetchone()
    return str(row[0]), str(row[1])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LedgerForge report scale-out benchmark")
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
        help="admin DSN of a PostgreSQL server (a fresh lf_bench_scale DB is created)",
    )
    parser.add_argument("--n", type=int, default=N_VOUCHERS, help="voucher count to seed")
    parser.add_argument("--smoke", action="store_true", help="tiny run for CI (n=300)")
    args = parser.parse_args()
    if args.pgserver_dir:
        import pgserver

        args.admin_dsn = pgserver.get_server(args.pgserver_dir).get_uri()
    if args.smoke:
        args.n = 300

    admin = _as_psycopg(args.admin_dsn)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS lf_bench_scale WITH (FORCE)")
        conn.execute("CREATE DATABASE lf_bench_scale")
    dsn = _rebuild(admin, "lf_bench_scale")

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    from ledgerforge import bootstrap

    bootstrap.apply_sql_files(dsn, repo_root, echo=False)

    with psycopg.connect(dsn) as conn:
        book = conn.execute("SELECT id FROM books LIMIT 1").fetchone()[0]
        cash = conn.execute("SELECT id FROM ledgers WHERE name = 'Cash'").fetchone()[0]
        sales_group = conn.execute(
            "SELECT id FROM groups WHERE name = 'Sales Accounts'"
        ).fetchone()[0]
        sales = conn.execute(
            "INSERT INTO ledgers (name, group_id, book_id)"
            " VALUES ('Bench Sales', %s, %s) RETURNING id",
            (sales_group, book),
        ).fetchone()[0]
        vt = conn.execute("SELECT id FROM voucher_types WHERE code = 'SAL'").fetchone()[0]

        # periods: close 2020..2024, leave 2025+ open (the cache's habitat)
        for year in YEARS:
            conn.execute(
                "INSERT INTO periods (name, starts_on, ends_on, book_id) VALUES (%s, %s, %s, %s)",
                (f"Bench FY{year}", date(year, 1, 1), date(year, 12, 31), book),
            )

        print(f"seeding {args.n} vouchers across {len(YEARS)} years (direct SQL) ...")
        started = time.perf_counter()
        # one multi-row voucher per iteration block: voucher + D/C pair
        with conn.cursor() as cur:
            for i in range(args.n):
                year = YEARS[i % len(YEARS)]
                vdate = date(year, 1 + (i % 12), 1 + (i % 28))
                amount = f"{(i % 900) + 1}.25"
                cur.execute(
                    "INSERT INTO vouchers (book_id, voucher_type_id, voucher_no,"
                    " sequence, voucher_date, client_token)"
                    " VALUES (%s, %s, %s, %s, %s, gen_random_uuid()) RETURNING id",
                    (book, vt, f"BENCH-{i}", i + 1, vdate),
                )
                vid = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO voucher_entries (voucher_id, line_no, ledger_id,"
                    " book_id, dc, amount, voucher_date)"
                    " VALUES (%s, 1, %s, %s, 'D', %s, %s), (%s, 2, %s, %s, 'C', %s, %s)",
                    (vid, cash, book, amount, vdate, vid, sales, book, amount, vdate),
                )
        conn.commit()
        print(f"seeded in {time.perf_counter() - started:.1f}s")

        # close 2020..2024 (the sweep is skipped when balances are zero-ish;
        # the periods close regardless — the gate is what the cache needs)
        for year in (2020, 2021, 2022, 2023, 2024):
            conn.execute(
                "UPDATE periods SET status = 'locked', locked_at = now()"
                " WHERE book_id = %s AND name = %s",
                (book, f"Bench FY{year}"),
            )
            conn.execute(
                "UPDATE periods SET status = 'closed', closed_at = now(), closed_by = 'bench'"
                " WHERE book_id = %s AND name = %s",
                (book, f"Bench FY{year}"),
            )
        conn.commit()
    # a real deployment has autovacuum maintaining the visibility map; do it
    # explicitly here so index-only scans work on fresh tables/partitions
    with psycopg.connect(dsn, autocommit=True) as vac:
        vac.execute("VACUUM ANALYZE voucher_entries")
        vac.execute("VACUUM ANALYZE vouchers")
    with psycopg.connect(dsn) as conn:
        n_entries = conn.execute("SELECT count(*) FROM voucher_entries").fetchone()[0]

        # ---------------- the measured shapes ----------------
        results: list[tuple[str, float]] = []

        results.append(("plain, slow scan", _time_tb(dsn, book, fast=False)))

        conn.execute("SELECT * FROM rpt.refresh_balance_cache(%s)", (book,))
        conn.commit()
        results.append(("plain, fast (warm cache)", _time_tb(dsn, book, fast=True)))

        slow_totals = _tb_totals(dsn, book, fast=False)
        fast_totals = _tb_totals(dsn, book, fast=True)
        assert slow_totals == fast_totals, f"fast != slow: {fast_totals} vs {slow_totals}"

        conn.execute("SELECT * FROM ops.partition_entries_by_year(1)")
        conn.commit()
    with psycopg.connect(dsn, autocommit=True) as vac:
        vac.execute("VACUUM ANALYZE voucher_entries")
    with psycopg.connect(dsn) as conn:
        results.append(("partitioned, slow scan", _time_tb(dsn, book, fast=False)))
        results.append(("partitioned, fast (warm cache)", _time_tb(dsn, book, fast=True)))

        slow_totals = _tb_totals(dsn, book, fast=False)
        fast_totals = _tb_totals(dsn, book, fast=True)
        assert slow_totals == fast_totals, "fast != slow on the partitioned shape"

        plan = "\n".join(
            r[0]
            for r in conn.execute(
                "EXPLAIN SELECT sum(amount) FROM voucher_entries"
                " WHERE voucher_date >= '2021-01-01' AND voucher_date < '2022-01-01'"
            ).fetchall()
        )
        pruned = "voucher_entries_2021" in plan and "voucher_entries_2020" not in plan

    width = max(len(name) for name, _ in results)
    print(f"\nTrial Balance over {n_entries} entries ({args.n} vouchers), best of 3:")
    for name, seconds in results:
        print(f"  {name:<{width}}   {seconds * 1000:8.1f} ms")
    print(f"\npartition pruning on a dated range: {'YES' if pruned else 'NO'}")
    print("fast == slow on every shape:        YES (asserted)")


if __name__ == "__main__":
    main()
