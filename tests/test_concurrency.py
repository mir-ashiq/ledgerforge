"""ADR-002 under fire: concurrent posting through the engine with threads.

8 workers x 40 vouchers on the SAME voucher type (worst case for the
numbering counter): all must post, numbering must be gapless, the Trial
Balance must be exact, and no deadlocks may occur."""

from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from ledgerforge import db
from ledgerforge.engine.posting import post_voucher
from ledgerforge.models import Ledger, Voucher, VoucherType
from ledgerforge.reporting import trial_balance
from ledgerforge.schemas import VoucherCreate, VoucherLineIn

N_THREADS = 8
PER_THREAD = 40
TOTAL = N_THREADS * PER_THREAD


def _worker(results, errors, session_factory, manager, settings, cash_id, sales_id, amounts):
    session = session_factory()
    try:
        for amount in amounts:
            payload = VoucherCreate(
                voucher_type="SAL",
                voucher_date=date.today(),
                client_token=uuid4(),
                entries=[
                    VoucherLineIn(ledger_id=cash_id, dc="D", amount=amount),
                    VoucherLineIn(ledger_id=sales_id, dc="C", amount=amount),
                ],
            )
            voucher, _created = post_voucher(session, payload, manager, settings)
            results.append((voucher.id, voucher.sequence, voucher.voucher_no))
    except Exception as exc:  # noqa: BLE001 — recorded and asserted below
        errors.append(exc)
    finally:
        session.close()


def test_gapless_under_threads(client, fresh_db, db_session, plugin_manager, settings):
    from conftest import create_ledger

    create_ledger(client, "Concurrency Sales", "Sales Accounts")
    cash_id = db_session.scalar(select(Ledger.id).where(Ledger.name == "Cash"))
    sales_id = db_session.scalar(select(Ledger.id).where(Ledger.name == "Concurrency Sales"))
    db_session.commit()

    # deterministic amounts per thread so the grand total is known exactly
    expected_total = Decimal(0)
    thread_amounts: list[list[Decimal]] = []
    for t in range(N_THREADS):
        amounts = []
        for j in range(PER_THREAD):
            amount = Decimal(f"{t * PER_THREAD + j + 1}.25")
            amounts.append(amount)
            expected_total += amount
        thread_amounts.append(amounts)

    factory = db.session_factory()
    results: list = []
    errors: list = []
    threads = [
        threading.Thread(
            target=_worker,
            args=(
                results,
                errors,
                factory,
                plugin_manager,
                settings,
                cash_id,
                sales_id,
                thread_amounts[t],
            ),
        )
        for t in range(N_THREADS)
    ]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    assert not errors, f"worker errors: {errors!r}"
    assert len(results) == TOTAL
    print(f"\n{TOTAL} vouchers in {elapsed:.2f}s = {TOTAL / elapsed:.0f} vouchers/s")

    # gapless numbering, no duplicates
    sequences = sorted(r[1] for r in results)
    assert sequences == list(range(1, TOTAL + 1)), "numbering must be strictly gapless"
    assert len({r[2] for r in results}) == TOTAL

    # everything durable in the database
    sal_ids = db_session.scalars(
        select(Voucher.id)
        .join(VoucherType, Voucher.voucher_type_id == VoucherType.id)
        .where(VoucherType.code == "SAL")
    ).all()
    assert len(sal_ids) == TOTAL

    # the whole book still balances exactly
    report = trial_balance(db_session, date.today())
    assert Decimal(report["totals"]["difference"]) == 0
    assert Decimal(report["totals"]["dr"]) == expected_total
    assert Decimal(report["totals"]["cr"]) == expected_total
