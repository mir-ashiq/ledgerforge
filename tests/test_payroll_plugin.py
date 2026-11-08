"""The payroll plugin pack (contrib/payroll.py): the third business document
pack, built purely on the runtime (ADR-012) — zero schema change, plugin
API 0.4 (Resolver.fy_start_month).

The promises under test (ADR-003 + ADR-010 + ADR-011 + ADR-012):
- ``meta.payroll`` run documents derive the TallyPrime payroll journal: per
  employee, Dr each earning (Indirect Expenses), Dr employer contributions
  and Cr their liabilities, Cr each withheld component (Duties & Taxes),
  Cr net pay to the employee's payable ledger (Provisions) — one voucher
  per run, balanced exactly, computed annotations round-tripping through
  ``GET /vouchers/{id}``.
- YTD is a **fiscal-year fold** (the book's fy_start_month): runs of the
  same FY accumulate, the previous FY's do not; ``ytd()`` replays the same
  fold the transformer ran. Payable is an **all-time balance** (accrued net
  minus payouts) — an accumulator windows, a balance never expires.
- One run per employee per period (policy-overridable); reversed runs don't
  count — an original + its reversal net to zero on YTD and payable.
- Payouts settle: Dr the employee's payable / Cr the bank; guarded by the
  all-time payable position (``policy:allow_overpay`` books a recoverable
  advance); a payout to a stranger is not a payroll payout.
- Concurrency: same-employee documents serialize through transaction-scoped
  advisory locks — concurrent duplicate runs cannot both pass the guard.
- Masters resolve/create through the book-scoped Resolver only; books are
  strangers (same employee in two books = two ledgers, two YTDs).
- Guards (all 422): malformed periods, duplicate employees/components, bad
  kinds, non-positive amounts, employer without a liability, withheld
  exceeding gross, document-mode conflicts, FX lines, future dates and
  future periods, employee names colliding with unrelated ledgers.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from conftest import TODAY

from ledgerforge.contrib import payroll


# ------------------------------------------------------------------ helpers
def default_headers(client) -> dict | None:
    """Once a second book exists, requests must name one: pin to the default
    book (the books-are-strangers test creates Zeta mid-module)."""
    books = client.get("/api/v1/books").json()
    if len(books) == 1:
        return None
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    return {"X-Book-Id": str(default_id)}


def _month(offset: int = 0) -> str:
    """A YYYY-MM period ``offset`` months from today (0 = this month)."""
    y, m = date.today().year, date.today().month + offset
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


PERIOD = _month(0)  # the current work month
PERIOD_PREV = _month(-1)  # the month before


def comp(name: str, amount: str, kind: str, **extra) -> dict:
    out = {"name": name, "amount": amount, "kind": kind}
    out.update(extra)
    return out


def employee_entry(employee: str, components: list[dict], **extra) -> dict:
    out = {"employee": employee, "components": components}
    out.update(extra)
    return out


def post_payroll(
    client,
    doc: dict,
    *,
    type_: str = "JV",
    date_: str | None = None,
    headers: dict | None = None,
    token=None,
    meta_extra: dict | None = None,
):
    """Post a payroll document. ``meta_extra`` lands at the TOP meta level
    (policy flags, other packs' keys for conflict probes)."""
    meta: dict = {"payroll": doc}
    if meta_extra:
        meta.update(meta_extra)
    payload = {
        "voucher_type": type_,
        "voucher_date": date_ or TODAY,
        "entries": [  # placeholders — the transformer replaces them
            {"ledger_name": "Cash", "dc": "D", "amount": "0.01"},
            {"ledger_name": "Cash", "dc": "C", "amount": "0.01"},
        ],
        "meta": meta,
    }
    if token is not None:
        payload["client_token"] = str(token)
    return client.post("/api/v1/vouchers", json=payload, headers=headers or default_headers(client))


def post_run(client, period: str, employees: list[dict], *, reference: str | None = None, **kw):
    doc: dict = {"mode": "run", "period": period, "employees": employees}
    if reference:
        doc["reference"] = reference
    return post_payroll(client, doc, **kw)


def simple_run(client, employee: str, period: str, *, gross="1000.00", withheld="100.00", **kw):
    """The canonical run: one earning, one withheld, one employer component
    sharing the withheld's liability ledger."""
    return post_run(
        client,
        period,
        [
            employee_entry(
                employee,
                [
                    comp("Basic Salary", gross, "earning"),
                    comp("PF Payable", withheld, "withheld"),
                    comp("Employer PF", "50.00", "employer", liability="PF Payable"),
                ],
            )
        ],
        **kw,
    )


def post_payout(client, employee: str, amount: str, *, bank="HDFC Bank", reference=None, **kw):
    doc: dict = {"mode": "payout", "employee": employee, "amount": amount, "bank": bank}
    if reference:
        doc["reference"] = reference
    return post_payroll(client, doc, **kw)


def lines_by_ledger(body: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in body["entries"]:
        out.setdefault(e["ledger_name"], []).append(e)
    return out


def computed_of(body: dict, employee: str) -> dict:
    return next(
        e for e in body["meta"]["payroll"]["computed"]["employees"] if e["employee"] == employee
    )


def ledger_groups(client, headers: dict | None = None) -> dict[str, str]:
    return {
        row["name"]: row["group_name"]
        for row in client.get("/api/v1/ledgers", headers=headers or default_headers(client)).json()
    }


def _resolver(db_session):
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    return Resolver(db_session, book)


def _fy_calendar(client) -> tuple[int, int]:
    """(fy label of today, fy start month) of the default book."""
    books = client.get("/api/v1/books").json()
    default = next(b for b in books if b["slug"] == "default")
    fy_start = default["fy_start_month"]
    today = date.today()
    fy = today.year if today.month >= fy_start else today.year - 1
    return fy, fy_start


# --------------------------------------------------------- the payroll run
def test_run_builds_tally_payroll_journal(client, fresh_db):
    """Two employees, all three component kinds: the pack derives the full
    payroll journal — per employee, Dr earnings and the employer expense, Cr
    the withheld and employer liabilities, Cr net pay to the employee's
    payable ledger — and the document round-trips (ADR-010)."""
    payroll.RUNS.clear()
    fy, _ = _fy_calendar(client)
    r = post_run(
        client,
        PERIOD,
        [
            employee_entry(
                "Asha Verma",
                [
                    comp("Basic Salary", "50000.00", "earning"),
                    comp("HRA", "20000.00", "earning"),
                    comp("PF Payable", "1800.00", "withheld"),
                    comp("Professional Tax Payable", "200.00", "withheld"),
                    comp("Employer PF", "1800.00", "employer", liability="PF Payable"),
                ],
            ),
            employee_entry(
                "Ravi Iyer",
                [
                    comp("Basic Salary", "30000.00", "earning"),
                    comp("PF Payable", "500.00", "withheld"),
                ],
            ),
        ],
        reference="run 1",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)

    # Asha: Dr 70000 earnings, Dr 1800 employer, Cr 2000 withheld, Cr 68000 net
    basic_dr = [ln for ln in lines["Basic Salary"] if ln["dc"] == "D"]
    assert sum(Decimal(ln["amount"]) for ln in basic_dr) == Decimal("80000.00")  # 50000 + 30000
    assert Decimal(lines["HRA"][0]["amount"]) == Decimal("20000.00")
    assert Decimal(lines["Employer PF"][0]["amount"]) == Decimal("1800.00")
    assert lines["Employer PF"][0]["dc"] == "D"
    pf_credits = [ln for ln in lines["PF Payable"] if ln["dc"] == "C"]
    assert sum(Decimal(ln["amount"]) for ln in pf_credits) == Decimal(
        "4100.00"
    )  # 1800 withheld + 1800 employer (Asha) + 500 withheld (Ravi)
    assert Decimal(lines["Professional Tax Payable"][0]["amount"]) == Decimal("200.00")

    asha_net = lines["Asha Verma"][0]
    assert asha_net["dc"] == "C"
    assert Decimal(asha_net["amount"]) == Decimal("68000.00")
    assert asha_net["narration"] == f"net pay — Asha Verma {PERIOD}; run 1"
    assert Decimal(lines["Ravi Iyer"][0]["amount"]) == Decimal("29500.00")

    # the engine's law: exact balance across the whole run
    dr = sum(Decimal(e["amount"]) for e in body["entries"] if e["dc"] == "D")
    cr = sum(Decimal(e["amount"]) for e in body["entries"] if e["dc"] == "C")
    assert dr == cr == Decimal("101800.00")  # 100000 gross + 1800 employer

    # computed annotations — the payslip data — persist, not echo
    asha = computed_of(body, "Asha Verma")
    assert asha["gross"] == "70000.00"
    assert asha["withheld"] == "2000.00"
    assert asha["employer"] == "1800.00"
    assert asha["net"] == "68000.00"
    assert asha["ytd"] == {
        "gross": "70000.00",
        "withheld": "2000.00",
        "employer": "1800.00",
        "net": "68000.00",
        "runs": 1,
    }
    assert asha["components"] == [
        {"name": "Basic Salary", "kind": "earning", "amount": "50000.00", "ytd": "50000.00"},
        {"name": "HRA", "kind": "earning", "amount": "20000.00", "ytd": "20000.00"},
        {"name": "PF Payable", "kind": "withheld", "amount": "1800.00", "ytd": "1800.00"},
        {
            "name": "Professional Tax Payable",
            "kind": "withheld",
            "amount": "200.00",
            "ytd": "200.00",
        },
        {"name": "Employer PF", "kind": "employer", "amount": "1800.00", "ytd": "1800.00"},
    ]
    assert body["meta"]["payroll"]["computed"]["total_net"] == "97500.00"
    assert body["meta"]["payroll"]["computed"]["fy"] == str(fy)

    got = client.get(f"/api/v1/vouchers/{body['id']}").json()
    assert got["meta"]["payroll"]["computed"] == body["meta"]["payroll"]["computed"]

    # masters land in the TallyPrime groups
    groups = ledger_groups(client)
    assert groups["Basic Salary"] == "Indirect Expenses"
    assert groups["HRA"] == "Indirect Expenses"
    assert groups["Employer PF"] == "Indirect Expenses"
    assert groups["PF Payable"] == "Duties & Taxes"
    assert groups["Professional Tax Payable"] == "Duties & Taxes"
    assert groups["Asha Verma"] == "Provisions"
    assert groups["Ravi Iyer"] == "Provisions"

    assert len(payroll.RUNS) == 1
    record = payroll.RUNS[0]
    assert record["kind"] == "run"
    assert record["voucher_no"] == body["voucher_no"]
    assert record["period"] == PERIOD
    assert record["employees"] == ["Asha Verma", "Ravi Iyer"]


def test_run_validations(client, fresh_db):
    """Every malformed run document is a 422 — loud, never a guess."""
    good_comp = comp("Basic Salary", "1000.00", "earning")
    cases: list[tuple[dict, str]] = [
        ({"mode": "run", "employees": [employee_entry("A", [good_comp])]}, "period"),
        (
            {"mode": "run", "period": "2026-1", "employees": [employee_entry("A", [good_comp])]},
            "period",
        ),
        (
            {"mode": "run", "period": "2026-13", "employees": [employee_entry("A", [good_comp])]},
            "period month",
        ),
        (
            {"mode": "run", "period": "2026/10", "employees": [employee_entry("A", [good_comp])]},
            "period must be YYYY-MM",
        ),
        ({"mode": "run", "period": PERIOD, "employees": []}, "employees"),
        ({"mode": "run", "period": PERIOD, "employees": ["nope"]}, "must be an object"),
        (
            {"mode": "run", "period": PERIOD, "employees": [employee_entry("", [good_comp])]},
            "employee",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", []), employee_entry("A", [])],
            },
            "components",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [good_comp]), employee_entry("A", [good_comp])],
            },
            "twice in this run",
        ),
        (
            {"mode": "run", "period": PERIOD, "employees": [employee_entry("A", ["x"])]},
            "must be an object",
        ),
        (
            {"mode": "nope", "period": PERIOD, "employees": [employee_entry("A", [good_comp])]},
            "mode",
        ),
        ({"period": PERIOD, "employees": [employee_entry("A", [good_comp])]}, "mode"),
        (
            {"mode": "run", "period": PERIOD, "employees": [employee_entry("A", [{"name": "X"}])]},
            "kind",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "1.00", "bonus")])],
            },
            "kind",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("", "1.00", "earning")])],
            },
            "name",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "0", "earning")])],
            },
            "> 0",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "-5", "earning")])],
            },
            "> 0",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "nan", "earning")])],
            },
            "finite",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "1.00", "employer")])],
            },
            "liability",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [
                    employee_entry(
                        "A", [comp("X", "1.00", "earning"), comp("X", "2.00", "withheld")]
                    )
                ],
            },
            "unique per employee",
        ),
        (
            {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("X", "10.00", "withheld")])],
            },
            # ADR-029 widened the guard's wording: recoveries reduce net pay
            # exactly like withholdings, so the message names both now.
            "recoveries exceed gross",
        ),
    ]
    for doc, expected in cases:
        r = post_payroll(client, doc)
        assert r.status_code == 422, (doc, r.text)
        assert expected in r.json()["detail"], (doc, r.json()["detail"])


def test_net_zero_employee_has_no_payable_line(client, fresh_db):
    """Fully withheld (gross == withheld): no net-pay line for that
    employee — the earnings and withheld lines carry the run."""
    r = post_run(
        client,
        PERIOD,
        [
            employee_entry(
                "A",
                [
                    comp("Basic Salary", "100.00", "earning"),
                    comp("PF Payable", "100.00", "withheld"),
                ],
            ),
            employee_entry("B", [comp("Basic Salary", "80.00", "earning")]),
        ],
    )
    assert r.status_code == 201, r.text
    lines = lines_by_ledger(r.json())
    assert "A" not in lines
    assert Decimal(lines["B"][0]["amount"]) == Decimal("80.00")
    assert computed_of(r.json(), "A")["net"] == "0.00"


# ----------------------------------------------------------------- YTD fold
def test_ytd_windows_by_fiscal_year(client, fresh_db, db_session):
    """The accumulator windows by the book's fiscal year: a run in the last
    month of the previous FY does not count toward the current FY's YTD —
    and the public ``ytd()`` replays exactly what the transformer folded."""
    fy, fy_start = _fy_calendar(client)
    prev_fy_last = f"{fy - 1:04d}-12" if fy_start == 1 else f"{fy:04d}-{fy_start - 1:02d}"
    first_of_fy = f"{fy:04d}-{fy_start:02d}"

    # a run in the previous fiscal year — a different accumulator
    assert simple_run(client, "Meera Nair", prev_fy_last).status_code == 201
    # the current FY's first month: its YTD sees nothing before it
    r = simple_run(client, "Meera Nair", first_of_fy)
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Meera Nair")["ytd"] == {
        "gross": "1000.00",
        "withheld": "100.00",
        "employer": "50.00",
        "net": "900.00",
        "runs": 1,
    }

    # this month's run — same FY as first_of_fy unless the FY just opened
    # (first_of_fy == PERIOD), in which case that run already covered it
    same_fy_runs = 1
    body = r.json()
    if first_of_fy != PERIOD:
        r = simple_run(client, "Meera Nair", PERIOD)
        assert r.status_code == 201, r.text
        same_fy_runs = 2
        body = r.json()
    ytd = computed_of(body, "Meera Nair")["ytd"]
    assert ytd == {
        "gross": f"{1000 * same_fy_runs}.00",
        "withheld": f"{100 * same_fy_runs}.00",
        "employer": f"{50 * same_fy_runs}.00",
        "net": f"{900 * same_fy_runs}.00",
        "runs": same_fy_runs,
    }
    assert (
        next(
            c for c in computed_of(body, "Meera Nair")["components"] if c["name"] == "Basic Salary"
        )["ytd"]
        == f"{1000 * same_fy_runs}.00"
    )

    # the public query agrees — same fold, replayed
    res = _resolver(db_session)
    view = payroll.ytd(res, "Meera Nair")
    assert view.gross == Decimal(f"{1000 * same_fy_runs}.00")
    assert view.withheld == Decimal(f"{100 * same_fy_runs}.00")
    assert view.net == Decimal(f"{900 * same_fy_runs}.00")
    assert view.runs == same_fy_runs
    # ... and the previous fiscal year is still there when asked for
    prev = payroll.ytd(res, "Meera Nair", fy=fy - 1)
    assert prev.gross == Decimal("1000.00")
    assert prev.runs == 1
    # an unknown employee has an empty year
    assert payroll.ytd(res, "Nobody").runs == 0


# ------------------------------------------------------------------- payout
def test_payout_settles_net_pay(client, fresh_db):
    payroll.RUNS.clear()
    assert simple_run(client, "Vikram Rao", PERIOD).status_code == 201

    r = post_payout(client, "Vikram Rao", "400.00", reference="NEFT-42")
    assert r.status_code == 201, r.text
    body = r.json()
    lines = lines_by_ledger(body)
    assert lines["Vikram Rao"][0]["dc"] == "D"
    assert Decimal(lines["Vikram Rao"][0]["amount"]) == Decimal("400.00")
    assert lines["HDFC Bank"][0]["dc"] == "C"
    assert lines["Vikram Rao"][0]["narration"] == "salary payout — Vikram Rao; NEFT-42"
    assert body["meta"]["payroll"]["computed"] == {"payable_after": "500.00"}
    assert ledger_groups(client)["HDFC Bank"] == "Bank Accounts"
    assert len(payroll.RUNS) == 2
    assert payroll.RUNS[1]["kind"] == "payout"
    assert payroll.RUNS[1]["employee"] == "Vikram Rao"

    # overpay is a guard, not a law: policy books the recoverable advance
    r = post_payout(client, "Vikram Rao", "600.00")
    assert r.status_code == 422
    assert "exceeds" in r.json()["detail"]

    r = post_payout(client, "Vikram Rao", "500.00")
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["payroll"]["computed"] == {"payable_after": "0.00"}

    r = post_payout(client, "Vikram Rao", "1.00")
    assert r.status_code == 422
    r = post_payout(client, "Vikram Rao", "1.00", meta_extra={"policy:allow_overpay": True})
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["payroll"]["computed"] == {"payable_after": "-1.00"}


def test_payout_requires_a_relationship(client, fresh_db):
    """A payout settles a payroll relationship; paying a stranger is a
    plain payment voucher."""
    r = post_payout(client, "Stranger", "100.00")
    assert r.status_code == 422
    assert "no payroll relationship" in r.json()["detail"]

    r = post_payroll(client, {"mode": "payout", "employee": "Stranger", "amount": "1.00"})
    assert r.status_code == 422
    assert "bank" in r.json()["detail"]


# -------------------------------------------------- one run per period &c.
def test_one_run_per_employee_per_period(client, fresh_db):
    assert simple_run(client, "Priya Shah", PERIOD).status_code == 201
    r = simple_run(client, "Priya Shah", PERIOD)
    assert r.status_code == 422
    assert "already has a run" in r.json()["detail"]

    # a different employee may run the same period
    assert simple_run(client, "Amit Jain", PERIOD).status_code == 201
    # a back-dated period is free again — and its YTD window only sees
    # runs up to that period (the later PERIOD run is excluded)
    r = simple_run(client, "Priya Shah", PERIOD_PREV)
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Priya Shah")["ytd"]["runs"] == 1

    # a documented off-cycle correction overrides the guard — and counts
    # (this run's window sees both prior periods)
    r = simple_run(client, "Priya Shah", PERIOD, meta_extra={"policy:allow_duplicate_run": True})
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Priya Shah")["ytd"]["runs"] == 3


def test_reversal_nets_out_run(client, fresh_db, db_session):
    """A reversed run counts nowhere: YTD and payable return to zero, and
    the freed period accepts a fresh run (documents pair off, ADR-010)."""
    run = simple_run(client, "Kabir Das", PERIOD)
    assert run.status_code == 201
    voucher_id = run.json()["id"]

    rev = client.post(f"/api/v1/vouchers/{voucher_id}/reverse", headers=default_headers(client))
    assert rev.status_code == 201, rev.text
    assert rev.json()["meta"] == {}

    res = _resolver(db_session)
    assert payroll.ytd(res, "Kabir Das").runs == 0
    assert payroll.ytd(res, "Kabir Das").gross == Decimal("0.00")
    position = payroll.payable(res, "Kabir Das")
    assert position.payable == Decimal("0.00")

    # the period is free again — the reversed pair counts nowhere
    r = simple_run(client, "Kabir Das", PERIOD)
    assert r.status_code == 201, r.text
    assert computed_of(r.json(), "Kabir Das")["ytd"]["runs"] == 1


def test_idempotent_replay_does_not_double_pay(client, fresh_db, db_session):
    payroll.RUNS.clear()
    token = uuid4()
    first = simple_run(client, "Tara Sen", PERIOD, token=token)
    assert first.status_code == 201
    again = simple_run(client, "Tara Sen", PERIOD, token=token)
    assert again.status_code == 200, again.text  # replay answers 200, not 201
    assert again.json()["id"] == first.json()["id"]
    assert payroll.ytd(_resolver(db_session), "Tara Sen").runs == 1
    assert len(payroll.RUNS) == 1


# --------------------------------------------------------- derived payable
def test_payable_helper_agrees_with_postings(client, fresh_db, db_session):
    """Accrued net is all-time; payouts are all-time; the difference is the
    payable — the same all-time fold the payout guard runs."""
    assert simple_run(client, "Nikhil Bose", PERIOD).status_code == 201  # net 900
    assert simple_run(client, "Nikhil Bose", PERIOD_PREV).status_code == 201
    assert post_payout(client, "Nikhil Bose", "1000.00").status_code == 201

    position = payroll.payable(_resolver(db_session), "Nikhil Bose")
    assert position.accrued == Decimal("1800.00")
    assert position.paid == Decimal("1000.00")
    assert position.payable == Decimal("800.00")


# ------------------------------------------------------------- conflicts
def test_document_mode_conflicts(client, fresh_db):
    for other in (
        {"gst": {"rate": "18"}},
        {
            "invoice": {
                "customer": "Acme",
                "items": [{"description": "X", "qty": "1", "rate": "1", "tax_rate": "0"}],
            }
        },
        {"receipt": {"customer": "Acme", "amount": "1.00"}},
        {
            "stock": {
                "movement": "in",
                "party": "Omega",
                "items": [{"item": "W", "qty": "1", "rate": "1"}],
            }
        },
    ):
        r = post_run(
            client,
            PERIOD,
            [employee_entry("A", [comp("Basic Salary", "1.00", "earning")])],
            meta_extra=other,
        )
        assert r.status_code == 422, (other, r.text)
        assert "mutually exclusive" in r.json()["detail"]


def test_fx_lines_rejected_with_payroll(client, fresh_db):
    payload = {
        "voucher_type": "JV",
        "voucher_date": TODAY,
        "entries": [
            {
                "ledger_name": "Cash",
                "dc": "D",
                "amount": "83.50",
                "currency": "USD",
                "amount_fx": "1.00",
                "fx_rate": "83.5000000000",
            },
            {"ledger_name": "Cash", "dc": "C", "amount": "83.50"},
        ],
        "meta": {
            "payroll": {
                "mode": "run",
                "period": PERIOD,
                "employees": [employee_entry("A", [comp("Basic Salary", "1.00", "earning")])],
            }
        },
    }
    r = client.post("/api/v1/vouchers", json=payload, headers=default_headers(client))
    assert r.status_code == 422
    assert "foreign-currency" in r.json()["detail"]


def test_future_dates_and_periods_blocked_unless_overridden(client, fresh_db):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = simple_run(client, "Elena Roy", PERIOD, date_=tomorrow)
    assert r.status_code == 422
    assert "future" in r.json()["detail"]

    # a run for a month that has not started — same flag, same escape
    future_period = _month(1)
    r = simple_run(client, "Elena Roy", future_period)
    assert r.status_code == 422
    assert "has not started" in r.json()["detail"]

    ok = simple_run(
        client,
        "Elena Roy",
        future_period,
        meta_extra={"policy:allow_future_document_dates": True},
    )
    assert ok.status_code == 201, ok.text


# ------------------------------------------------------- master placement
def test_employee_ledger_collision_guard(client, fresh_db):
    """An employee may not silently reuse an existing unrelated ledger: the
    seeded Cash ledger is not a payable account."""
    r = simple_run(client, "Cash", PERIOD)
    assert r.status_code == 422
    assert "collides with the existing ledger" in r.json()["detail"]

    r = post_run(
        client,
        PERIOD,
        [
            employee_entry(
                "Cash",
                [comp("Basic Salary", "1.00", "earning")],
                payable_ledger="Salaries Payable",
            )
        ],
    )
    assert r.status_code == 201, r.text
    assert ledger_groups(client)["Salaries Payable"] == "Provisions"


def test_component_group_overrides(client, fresh_db):
    """Explicit groups win: production wages can go to Direct Expenses, a
    loan recovery can sit under Loans & Advances (Asset)."""
    r = post_run(
        client,
        PERIOD,
        [
            employee_entry(
                "Farah Khan",
                [
                    comp("Production Wages", "500.00", "earning", group="Direct Expenses"),
                    comp(
                        "Salary Advance Recovery",
                        "50.00",
                        "withheld",
                        group="Loans & Advances (Asset)",
                    ),
                ],
                payable_group="Current Liabilities",
            )
        ],
    )
    assert r.status_code == 201, r.text
    groups = ledger_groups(client)
    assert groups["Production Wages"] == "Direct Expenses"
    assert groups["Salary Advance Recovery"] == "Loans & Advances (Asset)"
    assert groups["Farah Khan"] == "Current Liabilities"


# ---------------------------------------------------------- book isolation
def test_books_are_strangers(client, fresh_db):
    """Same employee, two companies: two payable ledgers, two independent
    YTD accumulators — the pack inherits ADR-009 isolation."""
    r = client.post("/api/v1/books", json={"name": "Zeta Ventures", "base_currency": "INR"})
    assert r.status_code == 201, r.text
    zeta_id = r.json()["book"]["id"]
    zeta = {"X-Book-Id": str(zeta_id)}
    books = client.get("/api/v1/books").json()
    default_id = next(b["id"] for b in books if b["slug"] == "default")
    default = {"X-Book-Id": str(default_id)}

    assert simple_run(client, "Zoya Ali", PERIOD_PREV, headers=default).status_code == 201
    zeta_run = simple_run(client, "Zoya Ali", PERIOD, headers=zeta, gross="2000.00")
    assert zeta_run.status_code == 201, zeta_run.text
    assert computed_of(zeta_run.json(), "Zoya Ali")["ytd"]["gross"] == "2000.00"

    # the later period's YTD accumulates within the default book only
    second_default = simple_run(client, "Zoya Ali", PERIOD, headers=default)
    assert second_default.status_code == 201, second_default.text
    assert computed_of(second_default.json(), "Zoya Ali")["ytd"]["gross"] == "2000.00"

    default_ledgers = {
        row["name"]: row["id"] for row in client.get("/api/v1/ledgers", headers=default).json()
    }
    zeta_ledgers = {
        row["name"]: row["id"] for row in client.get("/api/v1/ledgers", headers=zeta).json()
    }
    assert default_ledgers["Zoya Ali"] != zeta_ledgers["Zoya Ali"]

    # the duplicate-period guard is book-scoped: Zeta may run its own month
    assert simple_run(client, "Zoya Ali", PERIOD_PREV, headers=zeta).status_code == 201


# ------------------------------------------------------------ concurrency
def test_concurrent_duplicate_runs_serialize(client, fresh_db, db_session, settings):
    """Three concurrent runs, same employee, same period: exactly one
    posts. The advisory lock (resolver.lock, API 0.3) serializes the
    same-employee documents, so the one-run-per-period guard is airtight
    under races."""
    from sqlalchemy import select

    from ledgerforge import db
    from ledgerforge.contrib import example_gst, inventory, invoicing
    from ledgerforge.engine.posting import post_voucher as engine_post
    from ledgerforge.models import Book
    from ledgerforge.plugins import PluginManager
    from ledgerforge.schemas import VoucherCreate, VoucherLineIn

    manager = PluginManager()
    for module in (example_gst, invoicing, inventory, payroll):
        manager.load_module(module)

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    db_session.commit()

    session_factory = db.session_factory()
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=10)
            payload = VoucherCreate(
                voucher_type="JV",
                voucher_date=date.today(),
                client_token=uuid4(),
                entries=[
                    VoucherLineIn(ledger_name="Cash", dc="D", amount="1.00"),
                    VoucherLineIn(ledger_name="Cash", dc="C", amount="1.00"),
                ],
                meta={
                    "payroll": {
                        "mode": "run",
                        "period": PERIOD,
                        "employees": [
                            employee_entry(
                                "Race Runner", [comp("Basic Salary", "1000.00", "earning")]
                            )
                        ],
                    }
                },
            )
            voucher, _created = engine_post(session, payload, manager, settings, book)
            results.append(voucher.voucher_no)
        except Exception as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert len(results) == 1, (results, [str(e) for e in errors])
    assert len(errors) == 2
    assert all("already has a run" in str(e) for e in errors)

    view = payroll.ytd(_resolver(db_session), "Race Runner")
    assert view.runs == 1
    assert view.gross == Decimal("1000.00")


# ------------------------------------------------- the plugin API 0.4 fact
def test_resolver_exposes_fy_start_month(db_session):
    """Resolver.fy_start_month (plugin API 0.4, ADR-012): the one fact a
    year-to-date fold needs, read off the book the resolver is scoped to."""
    from sqlalchemy import select

    from ledgerforge.models import Book
    from ledgerforge.plugins import Resolver

    book = db_session.scalar(select(Book).where(Book.slug == "default"))
    assert book is not None
    resolver = Resolver(db_session, book)
    assert resolver.fy_start_month == book.fy_start_month == 4
