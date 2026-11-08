"""The payroll depth (ADR-029, roadmap Phase 8): attendance registers as
facts, attendance-derived pay, loans & advances with EMI recovery, the
full & final settlement, and the payslip outbox.

The promises under test (ADR-029, extending ADR-012):
- The attendance register imports as facts (migration 0011, the
  bank-statement discipline): idempotent by content fingerprint, a
  corrected register is a new snapshot (latest-batch-wins), day counts
  are validated against the calendar, and books are strangers.
- A ``per_day`` earning component derives its amount from the live
  register (``present + half × 0.5 + leave_paid`` × rate) — no register,
  no derivation (422, never a guess); the derivation rides the stored
  component and the computed block.
- A loan issues Dr the advance / Cr the bank with an exact-Decimal annuity
  schedule (rate 0 = interest-free; the final row absorbs rounding so
  principals sum to the principal); recovery components credit the
  advance ledger, reduce net pay, and can never cross the outstanding
  fold — named loans adopt their scheduled EMI automatically.
- The settlement books leave encashment, dues, withholdings and
  recoveries in one voucher; after it the employee is relieved (runs,
  loans and settlements 422; payouts stay legal); a reversal reopens the
  relationship.
- Every run queues one payslip envelope per employee to the
  ``LF_PAYROLL_OUTBOX`` JSONL — unset or unwritable means logged and
  skipped, never a failed posting. Core never sends mail.
- The pack reports (``payroll-attendance``, ``payroll-loans``) render
  JSON/CSV/PDF from the same folds the guards run.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from decimal import Decimal

from conftest import TODAY


# ------------------------------------------------------------------ helpers
def default_headers(client) -> dict | None:
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def _month(offset: int = 0) -> str:
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)
PERIOD_NEXT = _month(1)


def comp(name: str, amount: str | None, kind: str, **extra) -> dict:
    out: dict = {"name": name, "kind": kind}
    if amount is not None:
        out["amount"] = amount
    out.update(extra)
    return out


def entry(employee: str, components: list[dict], **extra) -> dict:
    out = {"employee": employee, "components": components}
    out.update(extra)
    return out


def post_payroll(client, doc: dict, *, meta_extra: dict | None = None, headers=None):
    meta: dict = {"payroll": doc}
    if meta_extra:
        meta.update(meta_extra)
    return client.post(
        "/api/v1/vouchers",
        json={
            "voucher_type": "JV",
            "voucher_date": TODAY,
            "entries": [
                {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
                {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
            ],
            "meta": meta,
        },
        headers=headers or default_headers(client),
    )


def post_register(client, period: str, entries: list[dict], *, source=None, headers=None):
    payload: dict = {"period": period, "entries": entries}
    if source:
        payload["source"] = source
    return client.post(
        "/api/v1/payroll/attendance",
        json=payload,
        headers=headers or default_headers(client),
    )


def attendance_row(employee: str, *, present=22, absent=0, half=0, leave_paid=0, note=None):
    row: dict = {
        "employee": employee,
        "present": present,
        "absent": absent,
        "half": half,
        "leave_paid": leave_paid,
    }
    if note:
        row["note"] = note
    return row


def loan_doc(
    employee: str,
    principal: str = "100000.00",
    *,
    rate: str = "9.0",
    months: int = 20,
    first_period: str | None = None,
    bank: str = "HDFC Current",
    **extra,
) -> dict:
    doc: dict = {
        "mode": "loan",
        "employee": employee,
        "principal": principal,
        "rate": rate,
        "months": months,
        "bank": bank,
    }
    if first_period:
        doc["first_period"] = first_period
    doc.update(extra)
    return doc


def issue_loan(client, employee: str, **kw):
    r = post_payroll(client, loan_doc(employee, **kw))
    assert r.status_code == 201, r.text
    body = r.json()
    doc = body["meta"]["payroll"]
    return body, doc


def entries_of(voucher: dict) -> list[dict]:
    return voucher.get("entries") or []


def lines_by_ledger(voucher: dict, name: str) -> list[dict]:
    return [e for e in entries_of(voucher) if e.get("ledger_name") == name]


def amount_of(voucher: dict, name: str, dc: str) -> Decimal:
    """The single line's amount on the named ledger — Decimal compare (the
    posted precision is the engine's, not the test's)."""
    matches = [e for e in lines_by_ledger(voucher, name) if e["dc"] == dc]
    assert len(matches) == 1, (name, dc, matches)
    return Decimal(matches[0]["amount"])


# ------------------------------------------------------ the attendance facts
def test_attendance_import_is_idempotent_and_latest_wins(client):
    r = post_register(client, PERIOD, [attendance_row("Asha Verma", present=20, absent=2)])
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["new"] == 1 and first["duplicates"] == 0 and first["revision"] == 1
    assert "batch_id" in first

    # the unchanged re-entry is a no-op — no batch, the receipt IS the answer
    r2 = post_register(client, PERIOD, [attendance_row("Asha Verma", present=20, absent=2)])
    assert r2.status_code == 201, r2.text
    assert r2.json()["new"] == 0 and "batch_id" not in r2.json()

    # a corrected register is a new snapshot; the live register moves with it
    r3 = post_register(
        client, PERIOD, [attendance_row("Asha Verma", present=22, absent=0)], source="correction"
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["revision"] == 2 and r3.json()["new"] == 1

    live = client.get(
        f"/api/v1/payroll/attendance?period={PERIOD}", headers=default_headers(client)
    ).json()
    assert len(live["entries"]) == 1
    assert live["entries"][0]["present"] == "22.0"  # NUMERIC(6,1): a half-day is 0.5

    history = client.get(
        f"/api/v1/payroll/attendance/history?period={PERIOD}", headers=default_headers(client)
    ).json()
    assert len(history["batches"]) == 2  # append-only history, latest wins


def test_attendance_validation_guards(client):
    cases = [
        ("bad period", {"period": "2026-13", "entries": [attendance_row("A")]}, "YYYY-MM"),
        ("empty", {"period": PERIOD, "entries": []}, "non-empty"),
        (
            "over the calendar",
            {"period": PERIOD, "entries": [attendance_row("A", present=31, absent=2)]},
            "occupy",
        ),
        (
            "duplicate employee",
            {
                "period": PERIOD,
                "entries": [attendance_row("A"), attendance_row("A", present=1)],
            },
            "twice",
        ),
        ("negative", {"period": PERIOD, "entries": [{"employee": "A", "present": -1}]}, ">= 0"),
    ]
    for label, payload, expected in cases:
        r = client.post("/api/v1/payroll/attendance", json=payload, headers=default_headers(client))
        assert r.status_code == 422, (label, payload, r.text)
        assert expected in r.json()["detail"], (label, r.json()["detail"])

    # dry_run validates and writes nothing
    r = post_register(client, PERIOD, [attendance_row("Asha Verma", present=22)], source="dry")
    assert r.status_code == 201
    r2 = client.post(
        "/api/v1/payroll/attendance",
        json={"period": PERIOD, "entries": [attendance_row("Dry Run", present=1)], "dry_run": True},
        headers=default_headers(client),
    )
    assert r2.status_code == 201 and r2.json()["dry_run"] is True
    live = client.get(
        f"/api/v1/payroll/attendance?period={PERIOD}", headers=default_headers(client)
    ).json()
    assert all(e["employee"] != "Dry Run" for e in live["entries"])


def test_attendance_books_are_strangers(client):
    post_register(client, PERIOD, [attendance_row("Asha Verma", present=10)])
    r = client.post("/api/v1/books", json={"name": "Zeta Books", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_headers = {"X-Book-Id": str(r.json()["book"]["id"])}

    zeta_live = client.get(
        f"/api/v1/payroll/attendance?period={PERIOD}", headers=zeta_headers
    ).json()
    assert zeta_live["entries"] == []  # the new book sees no rows

    post_register(client, PERIOD, [attendance_row("Asha Verma", present=22)], headers=zeta_headers)
    default_live = client.get(
        f"/api/v1/payroll/attendance?period={PERIOD}", headers=default_headers(client)
    ).json()
    assert default_live["entries"][0]["present"] == "10.0"  # untouched


# ----------------------------------------------- attendance-derived components
def test_per_day_component_derives_from_the_live_register(client):
    post_register(
        client,
        PERIOD,
        [attendance_row("Deepa Nair", present=21, absent=1, half=2, leave_paid=1)],
    )
    # payable days = 21 + 2×0.5 + 1 = 23
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry("Deepa Nair", [comp("Basic Salary", None, "earning", per_day="1500.00")])
            ],
        },
    )
    assert r.status_code == 201, r.text
    voucher = r.json()
    posted = voucher["meta"]["payroll"]

    # the stored component carries the resolved amount and its derivation
    stored = posted["employees"][0]["components"][0]
    assert stored["amount"] == "34500.00"  # 23 × 1500
    assert Decimal(stored["days"]) == Decimal("23") and stored["per_day"] == "1500.00"

    computed = posted["computed"]["employees"][0]
    assert computed["gross"] == "34500.00" and computed["net"] == "34500.00"

    # the journal: Dr Basic Salary 34500 / Cr Deepa Nair (payable) 34500
    assert amount_of(voucher, "Basic Salary", "D") == Decimal("34500.00")
    assert amount_of(voucher, "Deepa Nair", "C") == Decimal("34500.00")


def test_per_day_without_a_register_refuses(client):
    # a register for the period, but not for this employee
    post_register(client, PERIOD, [attendance_row("Someone Else", present=22)])

    cases = [
        # no register row for the employee → 422, never a guess
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [
                    entry("Farah Khan", [comp("Basic", None, "earning", per_day="100.00")])
                ],
            },
            "has no row for them",
        ),
        # amount AND per_day → ambiguous → 422
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [
                    entry(
                        "Someone Else",
                        [comp("Basic", "100.00", "earning", per_day="100.00")],
                    )
                ],
            },
            "never both",
        ),
        # zero payable days → pay for no days is a 422
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [
                    entry(
                        "Someone Else",
                        [comp("Basic", None, "earning", per_day="100.00")],
                    )
                ],
            },
            "0 payable days",
        ),
    ]
    for doc, expected in cases:
        # the zero-days case needs a register row with no payable days
        if "0 payable days" in expected:
            post_register(client, PERIOD, [attendance_row("Someone Else", present=0, absent=30)])
        r = post_payroll(client, doc)
        assert r.status_code == 422, (doc, r.text)
        assert expected in r.json()["detail"], (doc, r.json()["detail"])


# --------------------------------------------------------- loans & advances
def test_loan_issue_books_the_advance_and_the_schedule(client):
    body, doc = issue_loan(
        client, "Harish Menon", principal="100000.00", rate="9.0", months=20, first_period=PERIOD
    )

    # the journal: Dr "{employee} Advance" / Cr the bank, at the principal
    assert amount_of(body, "Harish Menon Advance", "D") == Decimal("100000.00")
    assert amount_of(body, "HDFC Current", "C") == Decimal("100000.00")

    # the schedule: exact annuity, principals sum to the principal, the
    # final row absorbs the rounding and closes at zero
    computed = doc["computed"]
    schedule = computed["schedule"]
    assert len(schedule) == 20
    assert schedule[0]["period"] == PERIOD
    assert schedule[-1]["closing"] == "0.00"
    assert sum(Decimal(row["principal"]) for row in schedule) == Decimal("100000.00")
    # the hand-computed annuity (independently verified): 100000 @ 9%/12
    # for 20 months → EMI 5403.06, first interest 750.00
    assert computed["emi"] == "5403.06"
    assert schedule[0]["interest"] == "750.00"
    assert Decimal(schedule[0]["principal"]) == Decimal("5403.06") - Decimal("750.00")
    assert computed["advance_ledger"] == "Harish Menon Advance"


def test_interest_free_advance_schedule(client):
    _, doc = issue_loan(
        client, "Ila Das", principal="50000.00", rate="0", months=10, first_period=PERIOD
    )
    computed = doc["computed"]
    assert computed["emi"] == "5000.00"  # P/n exactly
    assert all(Decimal(row["interest"]) == 0 for row in computed["schedule"])
    assert sum(Decimal(r["principal"]) for r in computed["schedule"]) == Decimal("50000.00")


def test_loan_position_and_recovery_in_runs(client):
    _, loan = issue_loan(
        client, "Asha Verma", principal="100000.00", months=20, first_period=PERIOD
    )
    loan_no = None
    # the voucher number rides the daybook entry; fetch from the listing
    listing = client.get("/api/v1/vouchers?type=JV", headers=default_headers(client)).json()
    for v in listing:
        d = (v.get("meta") or {}).get("payroll")
        if isinstance(d, dict) and d.get("mode") == "loan" and d.get("employee") == "Asha Verma":
            loan_no = v["voucher_no"]
    assert loan_no, "the loan voucher must be listed"

    # a run with an explicit recovery against the named loan
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Asha Verma",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", "4568.58", "recovery", loan=loan_no),
                    ],
                )
            ],
        },
    )
    assert r.status_code == 201, r.text
    voucher = r.json()

    # net pay = 20000 − 4568.58 = 15431.42 credited to the payable ledger
    assert amount_of(voucher, "Asha Verma", "C") == Decimal("15431.42")
    # the recovery credits the advance ledger, not a statutory liability
    assert amount_of(voucher, "Asha Verma Advance", "C") == Decimal("4568.58")

    # the computed block names the recovery; YTD recovered follows
    computed = voucher["meta"]["payroll"]["computed"]["employees"][0]
    assert computed["recovered"] == "4568.58"
    assert computed["net"] == "15431.42"

    # the fold: issued 100000, recovered 4568.58, outstanding 95431.42
    employees = client.get(
        "/api/v1/reports/pack/payroll-loans?format=json", headers=default_headers(client)
    ).json()["employees"]
    asha = next(e for e in employees if e["employee"] == "Asha Verma")
    assert asha["issued"] == "100000.00"
    assert asha["recovered"] == "4568.58"
    assert asha["outstanding"] == "95431.42"
    assert asha["loans"][0]["voucher_no"] == loan_no
    assert asha["loans"][0]["outstanding"] == "95431.42"


def test_auto_emi_recovery_adopts_the_schedule(client):
    _, loan = issue_loan(client, "Jay Rao", principal="100000.00", months=20, first_period=PERIOD)
    listing = client.get("/api/v1/vouchers?type=JV", headers=default_headers(client)).json()
    loan_no = next(
        v["voucher_no"]
        for v in listing
        if (v.get("meta") or {}).get("payroll", {}).get("mode") == "loan"
    )
    emi = loan["computed"]["emi"]

    # no amount, just the loan reference — the run adopts the scheduled EMI
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Jay Rao",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", None, "recovery", loan=loan_no),
                    ],
                )
            ],
        },
    )
    assert r.status_code == 201, r.text
    stored = r.json()["meta"]["payroll"]["employees"][0]["components"][1]
    assert stored["amount"] == emi

    # a period outside the schedule refuses — an installment that is not
    # due is never adopted
    r2 = post_payroll(
        client,
        {
            "mode": "run",
            "period": _month(-3),
            "employees": [
                entry(
                    "Jay Rao",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", None, "recovery", loan=loan_no),
                    ],
                )
            ],
        },
    )
    assert r2.status_code == 422, r2.text
    assert "no installment due" in r2.json()["detail"]


def test_recovery_guards_never_cross_the_outstanding(client):
    _, _loan = issue_loan(client, "Kiran Shah", principal="1000.00", months=4, first_period=PERIOD)

    # the aggregate ceiling: recovering more than was ever issued
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Kiran Shah",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", "1000.01", "recovery"),
                    ],
                )
            ],
        },
    )
    assert r.status_code == 422 and "exceed their loan outstanding" in r.json()["detail"]

    # a named loan that is not theirs
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Kiran Shah",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", "10.00", "recovery", loan="JV-9999"),
                    ],
                )
            ],
        },
    )
    assert r.status_code == 422 and "not a live loan document" in r.json()["detail"]

    # recovery against a fresh employee with no loan at all
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Stranger",
                    [
                        comp("Basic Salary", "20000.00", "earning"),
                        comp("Loan Recovery", "10.00", "recovery"),
                    ],
                )
            ],
        },
    )
    assert r.status_code == 422 and "exceed their loan outstanding" in r.json()["detail"]


def test_loan_reversal_restores_the_outstanding(client):
    body, _doc = issue_loan(
        client, "Lena Fernandes", principal="1000.00", months=4, first_period=PERIOD
    )
    vid = body["id"]
    r = client.post(f"/api/v1/vouchers/{vid}/reverse", headers=default_headers(client))
    assert r.status_code == 201, r.text

    employees = client.get(
        "/api/v1/reports/pack/payroll-loans?format=json", headers=default_headers(client)
    ).json()["employees"]
    # the reversed loan nets out of the fold — no employee with loans remains
    assert all(e["employee"] != "Lena Fernandes" for e in employees)


# ------------------------------------------------- the full & final settlement
def settlement_doc(employee: str, **extra) -> dict:
    doc: dict = {
        "mode": "settlement",
        "employee": employee,
        "period": PERIOD,
        "encashment": {"days": "12.5", "per_day": "1500.00"},
        "earnings": [{"name": "Sales Incentive", "amount": "10000.00"}],
        "withheld": [{"name": "TDS Payable", "amount": "2000.00"}],
    }
    doc.update(extra)
    return doc


def test_settlement_books_encashment_dues_and_recoveries(client):
    _, _loan = issue_loan(client, "Meera Joshi", principal="1000.00", months=4, first_period=PERIOD)
    listing = client.get("/api/v1/vouchers?type=JV", headers=default_headers(client)).json()
    loan_no = next(
        v["voucher_no"]
        for v in listing
        if (v.get("meta") or {}).get("payroll", {}).get("mode") == "loan"
    )

    # encashment 12.5 × 1500 = 18750; dues 10000; withheld 2000; recovery 500
    doc = settlement_doc(
        "Meera Joshi", recoveries=[{"name": "Loan Recovery", "amount": "500.00", "loan": loan_no}]
    )
    r = post_payroll(client, doc)
    assert r.status_code == 201, r.text
    voucher = r.json()

    # Dr Leave Encashment 18750, Dr Sales Incentive 10000
    assert amount_of(voucher, "Leave Encashment", "D") == Decimal("18750.00")
    assert amount_of(voucher, "Sales Incentive", "D") == Decimal("10000.00")
    # Cr TDS Payable 2000, Cr the advance 500, Cr net dues to the payable
    assert amount_of(voucher, "TDS Payable", "C") == Decimal("2000.00")
    assert amount_of(voucher, "Meera Joshi Advance", "C") == Decimal("500.00")
    assert amount_of(voucher, "Meera Joshi", "C") == Decimal("26250.00")  # 28750 − 2500

    computed = voucher["meta"]["payroll"]["computed"]
    assert computed["encashment"]["amount"] == "18750.00"
    assert computed["net"] == "26250.00"
    assert computed["loan_outstanding_after"] == "500.00"


def test_settlement_can_pay_the_dues_direct(client):
    # a run first, so the relationship exists and the payable carries dues
    post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Nikhil Roy", [comp("Basic Salary", "5000.00", "earning")])],
        },
    )
    r = post_payroll(
        client,
        {
            "mode": "settlement",
            "employee": "Nikhil Roy",
            "period": PERIOD,
            "earnings": [{"name": "Gratuity", "amount": "10000.00"}],
            "bank": "HDFC Current",
        },
    )
    assert r.status_code == 201, r.text
    voucher = r.json()
    # the net dues leave with the settlement itself — Cr the bank
    assert amount_of(voucher, "HDFC Current", "C") == Decimal("10000.00")
    assert voucher["meta"]["payroll"]["computed"]["paid_direct"] is True


def test_settlement_gates_and_reversal_reopens(client):
    # a relationship, then the settlement
    post_payroll(
        client,
        {
            "mode": "run",
            "period": _month(-1),
            "employees": [entry("Nina Pillai", [comp("Basic Salary", "5000.00", "earning")])],
        },
    )
    r = post_payroll(client, settlement_doc("Nina Pillai"))
    assert r.status_code == 201, r.text
    settlement_id = r.json()["id"]

    # duplicate settlement → 422 (policy escape works)
    r = post_payroll(client, settlement_doc("Nina Pillai"))
    assert r.status_code == 422 and "already has a full & final settlement" in r.json()["detail"]
    r = post_payroll(
        client,
        settlement_doc("Nina Pillai"),
        meta_extra={"policy:allow_duplicate_settlement": True},
    )
    assert r.status_code == 201, r.text
    client.post(f"/api/v1/vouchers/{r.json()['id']}/reverse", headers=default_headers(client))

    # relieved: runs, loans refuse…
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Nina Pillai", [comp("Basic Salary", "100.00", "earning")])],
        },
    )
    assert r.status_code == 422 and "relieved" in r.json()["detail"]
    r = post_payroll(client, loan_doc("Nina Pillai", principal="100.00"))
    assert r.status_code == 422 and "relieved" in r.json()["detail"]

    # …but payouts stay legal — the F&F dues are the whole point
    r = post_payroll(
        client,
        {"mode": "payout", "employee": "Nina Pillai", "amount": "26250.00", "bank": "HDFC Current"},
    )
    assert r.status_code == 201, r.text

    # the policy escape admits a genuinely-late correction
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Nina Pillai", [comp("Late Bonus", "100.00", "earning")])],
        },
        meta_extra={"policy:allow_post_settlement": True},
    )
    assert r.status_code == 201, r.text

    # the settlement's reversal reopens the relationship (a fresh period —
    # the policy-escaped PERIOD run already exists above)
    r = client.post(f"/api/v1/vouchers/{settlement_id}/reverse", headers=default_headers(client))
    assert r.status_code == 201, r.text
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": _month(-2),
            "employees": [entry("Nina Pillai", [comp("Basic Salary", "100.00", "earning")])],
        },
    )
    assert r.status_code == 201, r.text


def test_settlement_requires_a_relationship(client):
    r = post_payroll(client, settlement_doc("A Stranger"))
    assert r.status_code == 422 and "cannot open it" in r.json()["detail"]


def test_settlement_prints_as_the_statement(client):
    post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Omi Sen", [comp("Basic Salary", "5000.00", "earning")])],
        },
    )
    r = post_payroll(client, settlement_doc("Omi Sen"))
    vid = r.json()["id"]
    pdf = client.get(f"/api/v1/vouchers/{vid}/pdf", headers=default_headers(client))
    assert pdf.status_code == 200, pdf.text
    assert pdf.content[:4] == b"%PDF"


# ------------------------------------------------------------ the outbox
def test_run_queues_one_payslip_envelope_per_employee(client, tmp_path, monkeypatch):
    outbox = tmp_path / "payslips-outbox.jsonl"
    monkeypatch.setenv("LF_PAYROLL_OUTBOX", str(outbox))

    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [
                entry(
                    "Qara Shah",
                    [comp("Basic Salary", "1000.00", "earning")],
                    email="asha@example.com",
                ),
                entry("Ravi Kumar", [comp("Basic Salary", "900.00", "earning")]),
            ],
        },
    )
    assert r.status_code == 201, r.text

    lines = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert len(lines) == 2
    assert all(line["kind"] == "lf-payroll-payslip/1" for line in lines)
    by_name = {line["employee"]: line for line in lines}
    assert by_name["Qara Shah"]["email"] == "asha@example.com"
    assert by_name["Ravi Kumar"]["email"] is None  # blank, never invented
    assert by_name["Qara Shah"]["voucher_id"] == r.json()["id"]
    assert by_name["Qara Shah"]["period"] == PERIOD


def test_outbox_env_unset_means_no_file_and_no_failure(client, tmp_path, monkeypatch):
    monkeypatch.delenv("LF_PAYROLL_OUTBOX", raising=False)
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Sana Ali", [comp("Basic Salary", "1000.00", "earning")])],
        },
    )
    assert r.status_code == 201  # posting never depends on the outbox
    assert not (tmp_path / "payslips-outbox.jsonl").exists()


def test_unwritable_outbox_never_breaks_posting(client, tmp_path, monkeypatch):
    # a directory where the file should be → open() fails → logged, isolated
    blocker = tmp_path / "blocked.jsonl"
    blocker.mkdir()
    monkeypatch.setenv("LF_PAYROLL_OUTBOX", str(blocker))
    r = post_payroll(
        client,
        {
            "mode": "run",
            "period": PERIOD,
            "employees": [entry("Tara Bose", [comp("Basic Salary", "1000.00", "earning")])],
        },
    )
    assert r.status_code == 201, r.text


# ------------------------------------------------------------ the reports
def test_attendance_report_renders_json_csv_pdf(client):
    post_register(client, PERIOD, [attendance_row("Asha Verma", present=21, half=2, leave_paid=1)])
    headers = default_headers(client)

    data = client.get(
        f"/api/v1/reports/pack/payroll-attendance?period={PERIOD}&format=json", headers=headers
    ).json()
    assert Decimal(data["rows"][0]["payable_days"]) == Decimal("23")  # 21 + 1 + 1

    csv_text = client.get(
        f"/api/v1/reports/pack/payroll-attendance?period={PERIOD}&format=csv", headers=headers
    ).text
    assert "Asha Verma,21.0,0.0,2.0,1.0,23.00" in csv_text

    pdf = client.get(f"/api/v1/reports/pack/payroll-attendance?period={PERIOD}", headers=headers)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_loans_report_renders_json_csv_pdf(client):
    issue_loan(client, "Uma Devi", principal="10000.00", months=5, first_period=PERIOD)
    headers = default_headers(client)

    data = client.get("/api/v1/reports/pack/payroll-loans?format=json", headers=headers).json()
    asha = next(e for e in data["employees"] if e["employee"] == "Uma Devi")
    assert asha["outstanding"] == "10000.00"
    assert len(asha["loans"][0]["schedule"]) == 5

    csv_text = client.get("/api/v1/reports/pack/payroll-loans?format=csv", headers=headers).text
    assert "Uma Devi,10000.00,0.00,10000.00,1" in csv_text

    pdf = client.get("/api/v1/reports/pack/payroll-loans", headers=headers)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


# ------------------------------------------------------------- concurrency
def test_concurrent_recoveries_cannot_both_spend_the_outstanding(client):
    """The lock discipline (ADR-011/012/029): two runs recovering the same
    outstanding in the same period's duplicate-run-allowed shape — exactly
    one crosses the ceiling, the other 422s."""
    issue_loan(client, "Vikram Rao", principal="100.00", months=2, first_period=PERIOD)

    doc = {
        "mode": "run",
        "period": PERIOD,
        "employees": [
            entry(
                "Vikram Rao",
                [
                    comp("Basic Salary", "10000.00", "earning"),
                    comp("Loan Recovery", "100.00", "recovery"),
                ],
            )
        ],
    }
    results: list[int] = []
    barrier = threading.Barrier(2)

    def post():
        barrier.wait()
        r = post_payroll(
            client,
            doc,
            meta_extra={"policy:allow_duplicate_run": True},  # both are runs
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 422]
    # whichever succeeded, the outstanding is fully spent
    employees = client.get(
        "/api/v1/reports/pack/payroll-loans?format=json", headers=default_headers(client)
    ).json()["employees"]
    asha = next(e for e in employees if e["employee"] == "Vikram Rao")
    assert asha["outstanding"] == "0.00"
    assert asha["recovered"] == "100.00"
