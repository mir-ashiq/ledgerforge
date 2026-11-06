#!/usr/bin/env python3
"""Hierarchy benchmark: closure-table rollups vs recursive-CTE rollups.

Builds a synthetic book (Tally-style seed + 310 synthetic groups, ~3,400
ledgers, 60,000 vouchers / 120,000 entries), then measures:

  Q1  every group's subtree rollup — recursive CTE (adjacency only)
  Q2  every group's subtree rollup — closure table (one equi-join)
  Q3  full Trial Balance equivalent (ledger totals + group rollups), both ways

Each query runs 5 times after a warm-up; the median EXPLAIN ANALYZE
execution time is reported. Reproducible: `make bench` or
`python scripts/bench_hierarchy.py`.

Note: bulk data generation runs with session_replication_role=replica
(triggers off) because this benchmark measures READ performance — the
write path is measured separately by bench_posting.py and covered by the
test suite.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import statistics
import urllib.parse

import psycopg

RECURSIVE_ROLLUP = """
WITH RECURSIVE direct AS (
    SELECT l.group_id AS gid,
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END) AS amt
    FROM voucher_entries e
    JOIN ledgers l ON l.id = e.ledger_id
    GROUP BY l.group_id
),
chain AS (
    SELECT d.gid, d.amt, g.id AS target, g.parent_id
    FROM direct d JOIN groups g ON g.id = d.gid
    UNION ALL
    SELECT c.gid, c.amt, p.id, p.parent_id
    FROM chain c JOIN groups p ON p.id = c.parent_id
    WHERE c.parent_id IS NOT NULL
)
SELECT target AS group_id, SUM(amt) AS net
FROM chain
GROUP BY target
"""

CLOSURE_ROLLUP = """
WITH per_group AS (
    SELECT l.group_id AS gid,
           SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE -e.amount END) AS net
    FROM voucher_entries e
    JOIN ledgers l ON l.id = e.ledger_id
    GROUP BY l.group_id
)
SELECT c.ancestor_id AS group_id, SUM(pg.net) AS net
FROM per_group pg
JOIN group_closure c ON c.descendant_id = pg.gid
GROUP BY c.ancestor_id
"""

LEDGER_TOTALS = """
SELECT e.ledger_id,
       SUM(CASE WHEN e.dc = 'D' THEN e.amount ELSE 0 END) AS dr,
       SUM(CASE WHEN e.dc = 'C' THEN e.amount ELSE 0 END) AS cr
FROM voucher_entries e
JOIN vouchers v ON v.id = e.voucher_id
GROUP BY e.ledger_id
"""


def _rebuild(db_uri: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(db_uri)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


def _as_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def build_dataset(dsn: str, repo_root: str) -> dict:
    from ledgerforge import bootstrap

    bootstrap.apply_sql_files(dsn, pathlib.Path(repo_root), echo=False)

    with psycopg.connect(dsn) as conn:
        conn.execute("SET session_replication_role = replica")
        # 310 synthetic groups under Sundry Debtors (10 regions x 10 branches x 3 zones)
        conn.execute(
            """
            INSERT INTO groups (book_id, name, parent_id, nature)
            SELECT bk.id, 'Region ' || r || ' Branch ' || b, sd.id, 'asset'
            FROM generate_series(1, 10) r, generate_series(1, 10) b,
                 (SELECT id FROM groups WHERE name = 'Sundry Debtors') sd,
                 (SELECT id FROM books WHERE slug = 'default') bk
            """
        )
        conn.execute(
            """
            INSERT INTO groups (book_id, name, parent_id, nature)
            SELECT bk.id, parent.name || ' Zone ' || z, parent.id, 'asset'
            FROM groups parent, generate_series(1, 3) z,
                 (SELECT id FROM books WHERE slug = 'default') bk
            WHERE parent.name LIKE 'Region % Branch %'
            """
        )
        # ~3,400 ledgers spread over the synthetic leaf groups
        conn.execute(
            """
            INSERT INTO ledgers (book_id, name, group_id)
            SELECT bk.id, 'Customer ' || n, g.id
            FROM generate_series(1, 3400) n
            JOIN (
                SELECT id, row_number() OVER (ORDER BY id) AS rn
                FROM groups WHERE name LIKE '%Zone %'
            ) g ON g.rn = (n % (SELECT count(*) FROM groups WHERE name LIKE '%Zone %')) + 1,
                 (SELECT id FROM books WHERE slug = 'default') bk
            """
        )
        n_ledgers = conn.execute("SELECT count(*) FROM ledgers").fetchone()[0]
        # 60,000 vouchers / 120,000 entries over two fiscal years
        conn.execute(
            """
            INSERT INTO vouchers (book_id, client_token, voucher_type_id, voucher_no,
                                  sequence, voucher_date)
            SELECT bk.id, gen_random_uuid(), vt.id, 'BENCH-' || g, g,
                   DATE '2025-04-01' + (g % 560)
            FROM generate_series(1, 60000) g,
                 (SELECT id FROM voucher_types WHERE code = 'JV') vt,
                 (SELECT id FROM books WHERE slug = 'default') bk
            """
        )
        for line_no, prime, dc in ((1, 7919, "D"), (2, 104729, "C")):
            conn.execute(
                """
                INSERT INTO voucher_entries
                    (voucher_id, line_no, ledger_id, book_id, dc, amount, voucher_date)
                SELECT v.id, %s, l.id, v.book_id, %s, 100.50, v.voucher_date
                FROM vouchers v
                JOIN (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM ledgers) l
                  ON l.rn = (v.id * %s) %% %s + 1
                """,
                (line_no, dc, prime, n_ledgers),
            )
        conn.commit()
        stats = conn.execute(
            """
            SELECT (SELECT count(*) FROM groups),
                   (SELECT count(*) FROM ledgers),
                   (SELECT count(*) FROM voucher_entries)
            """
        ).fetchone()
    return {"groups": stats[0], "ledgers": stats[1], "entries": stats[2]}


def time_query(conn, sql: str, runs: int = 5) -> float:
    times = []
    for i in range(runs + 1):  # first run warms the cache
        row = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql).fetchone()
        ms = float(row[0][0]["Execution Time"])
        if i > 0:
            times.append(ms)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description="LedgerForge hierarchy benchmark")
    default_dsn = os.environ.get("LF_BENCH_ADMIN_URL") or _as_psycopg(
        os.environ.get("LF_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
    )
    parser.add_argument(
        "--pgserver-dir",
        default="",
        help="boot an embedded PostgreSQL in this directory (requires the pgserver package)",
    )
    parser.add_argument(
        "--admin-dsn",
        default=default_dsn,
        help="admin DSN of a PostgreSQL server (a fresh lf_bench DB is created)",
    )
    args = parser.parse_args()
    if args.pgserver_dir:
        import pgserver

        args.admin_dsn = pgserver.get_server(args.pgserver_dir).get_uri()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    admin = _as_psycopg(args.admin_dsn)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS lf_bench WITH (FORCE)")
        conn.execute("CREATE DATABASE lf_bench")
    bench_dsn = _rebuild(admin, "lf_bench")

    stats = build_dataset(bench_dsn, repo_root)
    print(
        f"dataset: {stats['groups']} groups, {stats['ledgers']} ledgers, {stats['entries']} entries"
    )

    with psycopg.connect(bench_dsn) as conn:
        rec_rollup = time_query(conn, RECURSIVE_ROLLUP)
        clo_rollup = time_query(conn, CLOSURE_ROLLUP)
        ledger_totals_ms = time_query(conn, LEDGER_TOTALS)
        rec_tb = rec_rollup + ledger_totals_ms
        clo_tb = clo_rollup + ledger_totals_ms

    print()
    print("| Query | Recursive CTE | Closure table | Speedup |")
    print("|---|---|---|---|")
    print(
        f"| Whole-tree group rollup | {rec_rollup:.0f} ms | {clo_rollup:.0f} ms "
        f"| **{rec_rollup / clo_rollup:.1f}x** |"
    )
    print(
        f"| Trial Balance (ledger totals + rollup) | {rec_tb:.0f} ms | {clo_tb:.0f} ms "
        f"| **{rec_tb / clo_tb:.1f}x** |"
    )


if __name__ == "__main__":
    main()
