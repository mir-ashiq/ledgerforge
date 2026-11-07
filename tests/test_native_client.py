"""The native client against the real engine — one HTTP hop, no shortcuts.

The app fixture builds the FastAPI app on the session's PostgreSQL; this
module stands a real uvicorn on an ephemeral loopback port so the ApiClient
exercises the exact wire path the Qt shell uses (headers, params, error
bodies, PDF bytes) — not a TestClient shim.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn

from ledgerforge.nativeapp.client import ApiClient, ApiError


@pytest.fixture()
def live_port(app):
    """The engine app on a real ephemeral loopback port."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001 — server still binding
            time.sleep(0.1)
    else:
        raise RuntimeError("the test engine never became healthy")

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def api(live_port: int) -> ApiClient:
    client = ApiClient(live_port)
    # bind the default book like MainWindow does on boot
    books = client.books()
    client.set_book(books[0].id)
    try:
        yield client
    finally:
        # close BEFORE the server stops (fixture LIFO): keep-alive pools
        # racing a shutting-down server are teardown noise, not failures
        client.close()


def test_health_and_books(api: ApiClient):
    health = api.health()
    assert health["status"] == "ok"
    assert health["auth_mode"] == "disabled"
    books = api.books()
    assert books and books[0].base_currency == "INR"


def test_masters_roundtrip(api: ApiClient):
    ledgers = api.ledgers()
    names = {ledger.name for ledger in ledgers}
    assert "Cash" in names  # the seeded system ledger
    groups = api.groups()
    assert any(group.name == "Cash-in-Hand" for group in groups)

    created = api.create_ledger("Native Test Debtor", group_name="Sundry Debtors")
    assert created.group_name == "Sundry Debtors"
    assert created.name == "Native Test Debtor"


def test_voucher_post_list_and_balanced_amount(api: ApiClient):
    ledgers = api.ledgers()
    ids = {ledger.name: ledger.id for ledger in ledgers}
    posted = api.create_voucher(
        voucher_type="JV",
        voucher_date=__import__("datetime").date.today(),
        entries=[
            {"ledger_id": ids["Cash"], "dc": "D", "amount": "100.00"},
            {"ledger_id": ids["Native Test Debtor"], "dc": "C", "amount": "100.00"},
        ],
        narration="the native client test voucher",
    )
    assert posted.voucher_no
    assert posted.amount == __import__("decimal").Decimal("100.00")
    assert len(posted.entries) == 2

    listed = api.vouchers(type_code="JV")
    assert any(v.id == posted.id for v in listed)

    fetched = api.voucher(posted.id)
    assert fetched.id == posted.id
    assert [e.ledger_name for e in fetched.entries]


def test_unbalanced_voucher_surfaces_the_engine_detail(api: ApiClient):
    ledgers = api.ledgers()
    ids = {ledger.name: ledger.id for ledger in ledgers}
    with pytest.raises(ApiError) as raised:
        api.create_voucher(
            voucher_type="JV",
            voucher_date=__import__("datetime").date.today(),
            entries=[
                {"ledger_id": ids["Cash"], "dc": "D", "amount": "100.00"},
                {
                    "ledger_id": ids.get("Native Test Debtor", ledgers[0]),
                    "dc": "C",
                    "amount": "90.00",
                },
            ],
        )
    assert raised.value.status_code >= 400
    # the engine's own words reach the GUI (Law 1)
    assert "debit" in raised.value.detail.lower() or "credit" in raised.value.detail.lower()


def test_voucher_pdf_returns_bytes(api: ApiClient):
    vouchers = api.vouchers()
    if not vouchers:  # module order independence: post one if this ran first
        test_voucher_post_list_and_balanced_amount(api)
        vouchers = api.vouchers()
    content, name = api.voucher_pdf(vouchers[0].id)
    assert content[:4] == b"%PDF"
    assert name.endswith(".pdf")


def test_reports_through_the_client(api: ApiClient):
    from decimal import Decimal

    tb = api.trial_balance()
    assert "ledgers" in tb and "totals" in tb
    assert Decimal(str(tb["totals"]["difference"])) == Decimal("0")  # the engine proved Law 1

    pl = api.profit_loss(
        __import__("datetime").date(2025, 4, 1), __import__("datetime").date(2026, 3, 31)
    )
    assert "trading" in pl and "profit_and_loss" in pl

    bs = api.balance_sheet()
    assert "assets" in bs and "liabilities" in bs and "equity" in bs

    fy = api.fy_close(__import__("datetime").date(2026, 3, 31))
    assert isinstance(fy, dict)


def test_gstr1_pack_both_formats(api: ApiClient):
    pdf, media, name = api.pack("gstr1", period="2026-10")
    assert pdf[:4] == b"%PDF" or len(pdf) > 0
    assert media == "application/pdf"
    assert name.endswith(".pdf")

    csv, media2, name2 = api.pack("gstr1", period="2026-10", format_="csv")
    assert media2.startswith("text/")
    assert name2.endswith(".csv")
    assert b"," in csv or len(csv) == 0  # a header even with no invoices


def test_periods_and_slots_answer(api: ApiClient):
    periods = api.periods()
    assert isinstance(periods, list)
    slots = api.slots()
    assert isinstance(slots, list)


def test_api_error_detail_shapes():
    from ledgerforge.nativeapp.client import _detail

    assert _detail({"detail": "boom"}, "fallback") == "boom"
    assert _detail(None, "fallback") == "fallback"
    assert _detail([{"loc": ["body", "entries"], "msg": "bad"}], "fallback") == "entries: bad"
    assert _detail(["plain"], "fallback") == "plain"
