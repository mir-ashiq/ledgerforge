#!/usr/bin/env python3
"""Posting-throughput benchmark (ADR-002).

N worker threads post real vouchers through the engine's public
``post_voucher`` API (same code path as the HTTP endpoint) against a fresh
benchmark database, all on the SAME voucher type — the worst case for the
gapless numbering counter. Reports throughput, verifies gaplessness and
the exact Trial Balance.

Reproducible: `make bench` or `python scripts/bench_posting.py`.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
import urllib.parse
from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg

N_THREADS = 8
PER_THREAD = 60
TOTAL = N_THREADS * PER_THREAD


def _rebuild(db_uri: str, dbname: str) -> str:
    parts = urllib.parse.urlsplit(db_uri)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{dbname}"))


def _as_psycopg(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _worker(results, errors, session_factory, manager, settings, cash_id, sales_id):
    from ledgerforge.engine.posting import post_voucher
    from ledgerforge.schemas import VoucherCreate, VoucherLineIn

    session = session_factory()
    try:
        for j in range(PER_THREAD):
            amount = f"{j + 1}.25"
            payload = VoucherCreate(
                voucher_type="SAL",
                voucher_date=date.today(),
                client_token=uuid4(),
                entries=[
                    VoucherLineIn(ledger_id=cash_id, dc="D", amount=amount),
                    VoucherLineIn(ledger_id=sales_id, dc="C", amount=amount),
                ],
            )
            voucher, _ = post_voucher(session, payload, manager, settings)
            results.append(voucher.sequence)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        session.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LedgerForge posting benchmark")
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
        help="admin DSN of a PostgreSQL server (a fresh lf_bench_post DB is created)",
    )
    args = parser.parse_args()
    if args.pgserver_dir:
        import pgserver

        args.admin_dsn = pgserver.get_server(args.pgserver_dir).get_uri()

    admin = _as_psycopg(args.admin_dsn)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS lf_bench_post WITH (FORCE)")
        conn.execute("CREATE DATABASE lf_bench_post")
    bench_dsn = _rebuild(admin, "lf_bench_post")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from sqlalchemy import select

    from ledgerforge import bootstrap, db
    from ledgerforge.contrib import example_gst
    from ledgerforge.models import Group, Ledger
    from ledgerforge.plugins import PluginManager
    from ledgerforge.reporting import trial_balance
    from ledgerforge.settings import get_settings

    bootstrap.apply_sql_files(bench_dsn, pathlib.Path(repo_root), echo=False)
    os.environ["LF_DATABASE_URL"] = bench_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    get_settings.cache_clear()
    db.reset_engine()

    session = db.session_factory()()
    sales_group = session.scalar(select(Group).where(Group.name == "Sales Accounts"))
    session.add(Ledger(name="Bench Sales", group_id=sales_group.id))
    session.commit()
    cash_id = session.scalar(select(Ledger.id).where(Ledger.name == "Cash"))
    sales_id = session.scalar(select(Ledger.id).where(Ledger.name == "Bench Sales"))
    session.close()

    manager = PluginManager()
    manager.load_module(example_gst)
    settings = get_settings()

    results: list[int] = []
    errors: list = []
    factory = db.session_factory()
    threads = [
        threading.Thread(
            target=_worker,
            args=(results, errors, factory, manager, settings, cash_id, sales_id),
        )
        for _ in range(N_THREADS)
    ]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    assert not errors, f"errors: {errors!r}"
    assert len(results) == TOTAL
    assert sorted(results) == list(range(1, TOTAL + 1)), "numbering must be gapless"

    check = db.session_factory()()
    report = trial_balance(check, date.today())
    check.close()
    assert Decimal(report["totals"]["difference"]) == 0

    print(f"threads: {N_THREADS}, vouchers/thread: {PER_THREAD}")
    print(f"posted:  {TOTAL}/{TOTAL} (0 failures, 0 deadlocks, gapless 1..{TOTAL})")
    print(f"elapsed: {elapsed:.2f}s")
    print(f"throughput: {TOTAL / elapsed:.0f} vouchers/s (single voucher type counter)")
    print(f"trial balance difference: {report['totals']['difference']} (exact)")


if __name__ == "__main__":
    main()
