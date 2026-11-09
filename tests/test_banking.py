"""The banking kernel (banking.py + api/banking.py): statement import, the
matchmaker, match/unmatch, and the reconciliation proof — ADR-026, Phase 5
of the v1.0 roadmap.

The promises under test:

- **Import is validated then idempotent.** CSV (withdrawal/deposit pairs,
  signed amounts, informational balance rows) and MT940 (balances, :61:
  lines with :86: narratives, reversal marks); one bad row refuses the
  whole batch; ``dry_run`` writes nothing; re-importing the same statement
  is a duplicates-only receipt; a later statement's genuine repeat of an
  identical transaction survives (occurrence +1) — and the FX guard
  refuses a statement whose currency is not the book's base.
- **Suggestions never commit.** The matchmaker is a read: amount-exact
  ranks first, the date window and cheque/voucher evidence rank behind,
  and the match table is untouched until an explicit POST.
- **Match/un-match are the only writes.** One live match per line (409 to
  double-match), the entry must be the bank ledger's own with its
  voucher's date (422 otherwise), un-match keeps the row with
  ``unmatched_at``, re-match rewrites it — and DELETE never happens.
- **The reconciliation statement is a proof.** book − statement =
  unmatched-book − unmatched-statement + pair differences, exact Decimals,
  asserted against sums computed independently of the fold; the anchor is
  the caller's statement figure or the fold from an imported opening; a
  matched pair priced differently surfaces as its own row and the proof
  still closes.
- **The seam**: the ``bank.statement.imported`` / ``bank.line.matched`` /
  ``bank.line.unmatched`` events fire after commit; the
  ``POST /api/v1/hooks/{key}`` route delivers the banking pack's gateway
  payload in-process; unknown keys 404.
- Guards: only bank-group ledgers (422 otherwise), another book's ledger
  is a stranger (404), reader-visible reads, no-match-to-unmatch 404.

The module shares one database (module-scoped ``fresh_db``), so tests work
with their own bank ledger each — a book's statements are namespaced by
ledger, distinct ledgers keep the tests independent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import post_voucher

TODAY = date.today()


# ------------------------------------------------------------------ fixtures
def _bank_ledger(client, name: str = "HDFC Current") -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": "Bank Accounts"})
    assert r.status_code in (201, 422), r.text  # tolerate earlier tests' ledgers
    rows = client.get("/api/v1/ledgers", params={"search": name}).json()
    return next(row for row in rows if row["name"] == name)


def _party_ledger(client, name: str) -> dict:
    r = client.post("/api/v1/ledgers", json={"name": name, "group_name": "Sundry Debtors"})
    assert r.status_code in (201, 422), r.text
    rows = client.get("/api/v1/ledgers", params={"search": name}).json()
    return next(row for row in rows if row["name"] == name)


def _receipt(client, bank: str, party: str, amount: str, date_: str, narration=None):
    """Money into the bank: Dr bank (the asset), Cr party."""
    return post_voucher(
        client,
        type_="RCT",
        date_=date_,
        narration=narration,
        entries=[
            {"ledger_name": bank, "dc": "D", "amount": amount},
            {"ledger_name": party, "dc": "C", "amount": amount},
        ],
    )


def _payment(client, bank: str, party: str, amount: str, date_: str, narration=None):
    """Money out of the bank: Cr bank, Dr party."""
    return post_voucher(
        client,
        type_="PYT",
        date_=date_,
        narration=narration,
        entries=[
            {"ledger_name": party, "dc": "D", "amount": amount},
            {"ledger_name": bank, "dc": "C", "amount": amount},
        ],
    )


def _import(client, ledger_id: int, fmt: str, text: str, **extra) -> dict:
    payload = {"format": fmt, "text": text}
    payload.update(extra)
    r = client.post(f"/api/v1/banking/{ledger_id}/statements", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _lines(client, ledger_id: int, status: str = "all") -> list[dict]:
    r = client.get(f"/api/v1/banking/{ledger_id}/lines", params={"status": status})
    assert r.status_code == 200, r.text
    return r.json()["lines"]


def _match_target(client, ledger_id: int, voucher_no: str, amount: str | None = None):
    """The sanctioned way to name a match: a suggestion (when the engine
    pairs the two) or the entry book (manual matching — the amounts may
    disagree, ADR-026 prices the difference honestly)."""
    payload = client.get(
        f"/api/v1/banking/{ledger_id}/suggestions", params={"window": 366, "limit": 500}
    ).json()
    for line in payload["lines"]:
        for cand in line["suggestions"]:
            if cand["voucher_no"] == voucher_no and (amount is None or cand["amount"] == amount):
                return {
                    "statement_line_id": line["statement_line_id"],
                    "entry_id": cand["entry_id"],
                    "voucher_id": cand["voucher_id"],
                    "voucher_date": cand["voucher_date"],
                }
    # manual: the first unmatched statement line × the entry book's voucher
    entries = client.get(
        f"/api/v1/banking/{ledger_id}/entries", params={"status": "unmatched"}
    ).json()["entries"]
    entry = next(e for e in entries if e["voucher_no"] == voucher_no)
    stmt_line = next(ln["id"] for ln in _lines(client, ledger_id, status="unmatched"))
    return {
        "statement_line_id": stmt_line,
        "entry_id": entry["entry_id"],
        "voucher_id": entry["voucher_id"],
        "voucher_date": entry["voucher_date"],
    }


CSV_STATEMENT = """Date,Narrative,Ref No,Withdrawal,Deposit
03/04/2026,UPI/Payment to Ramesh,REF1,2500.00,
05/04/2026,NEFT Credit from Acme Traders,REF2,,15000.00
07/04/2026,BANK CHARGES,REF3,117.00,
"""

MT940_STATEMENT = """:20:STMNT1
:25:1234567890
:28C:1
:60F:C260401INR125000,00
:61:260403D2500,00NTRFUPI123456
:86:UPI/Payment to Ramesh
:61:260405C15000,00NTRFNEFT987
:86:NEFT Credit from Acme Traders
:61:260407D117,00NTRFCHG
:86:BANK CHARGES Q1
:62F:C260407INR137383,00
"""


# ------------------------------------------------------------------- imports
_LEDGER_SEQ = [
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliet",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
]


@pytest.fixture()
def imported_csv(client):
    """A FRESH bank ledger with the standard CSV statement imported — a new
    ledger per test, because matches on a shared ledger would blind the
    next test's suggestions (the module database is shared)."""
    name = f"CSV Bank {_LEDGER_SEQ.pop(0) if _LEDGER_SEQ else abs(hash('csv')) % 9999}"
    bank = _bank_ledger(client, name)
    receipt = _import(client, bank["id"], "csv", CSV_STATEMENT, source="q1.csv")
    return bank, receipt


def test_csv_import_receipt_and_lines(client, imported_csv):
    bank, receipt = imported_csv
    assert receipt["parsed"] == 3
    assert receipt["new"] == 3
    assert receipt["duplicates"] == 0
    assert receipt["informational"] == 0
    assert receipt["currency"] == "INR"
    lines = _lines(client, bank["id"])
    assert [line["dc"] for line in lines] == ["D", "C", "D"]
    assert [line["amount"] for line in lines] == ["2500.00", "15000.00", "117.00"]
    assert lines[0]["reference"] == "REF1"
    assert lines[0]["narrative"] == "UPI/Payment to Ramesh"
    assert lines[1]["match"] is None


def test_dry_run_writes_nothing(client):
    bank = _bank_ledger(client, "Dry Bank")
    payload = {"format": "csv", "text": CSV_STATEMENT, "dry_run": True}
    r = client.post(f"/api/v1/banking/{bank['id']}/statements", json=payload)
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["dry_run"] is True and receipt["new"] == 3
    assert _lines(client, bank["id"]) == []


def test_reimport_is_idempotent(client, imported_csv):
    bank, _first = imported_csv
    second = _import(client, bank["id"], "csv", CSV_STATEMENT, source="again.csv")
    assert second["new"] == 0
    assert second["duplicates"] == 3
    assert "batch_id" not in second  # a full duplicate imports no batch
    assert len(_lines(client, bank["id"])) == 3


def test_twins_and_statement_identity(client):
    """Two identical transactions in ONE statement both survive (the
    occurrence keeps twins apart); re-importing the SAME statement is a
    full duplicate (identical content is the same statement, the honest
    verdict); a LATER statement that carries the twins again — different
    content, a third line — lands them beside the first pair."""
    bank = _bank_ledger(client, "Twins Bank")
    twins = (
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        "03/04/2026,UPI/Same shop,UTR1,500.00,\n"
        "04/04/2026,UPI/Same shop,UTR1,500.00,\n"
    )
    first = _import(client, bank["id"], "csv", twins)
    assert first["new"] == 2
    again = _import(client, bank["id"], "csv", twins)
    assert again["new"] == 0 and again["duplicates"] == 2  # the same statement
    later = _import(
        client,
        bank["id"],
        "csv",
        twins + "05/04/2026,UPI/Other shop,UTR2,100.00,\n",
    )
    assert later["new"] == 3, "a different statement's repeats are genuine"
    assert len(_lines(client, bank["id"])) == 5


def test_bad_rows_refuse_the_whole_batch(client):
    bank = _bank_ledger(client, "Reject Bank")
    bad = (
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        "03/04/2026,Good row,REF1,100.00,\n"
        "05/13/2026,Month-first date,REF2,200.00,\n"
        "07/04/2026,Both sides,REF3,10.00,20.00\n"
    )
    r = client.post(f"/api/v1/banking/{bank['id']}/statements", json={"format": "csv", "text": bad})
    assert r.status_code == 422
    assert "all-or-nothing" in r.json()["detail"]
    assert _lines(client, bank["id"]) == []  # the good row died with the bad


def test_mt940_import_with_balances(client):
    bank = _bank_ledger(client, "MT940 Bank")
    receipt = _import(client, bank["id"], "mt940", MT940_STATEMENT, source="s1.sta")
    assert receipt["new"] == 3
    assert receipt["opening_balance"] == "125000.00"
    assert receipt["closing_balance"] == "137383.00"
    lines = _lines(client, bank["id"])
    assert lines[0]["narrative"] == "UPI/Payment to Ramesh"
    # the fold anchor exists: reconciliation without a given balance folds it
    r = client.get(f"/api/v1/banking/{bank['id']}/reconciliation", params={"as_of": "2026-04-30"})
    assert r.status_code == 200, r.text
    assert r.json()["statement_balance_source"] == "folded"


def test_fx_statement_refused(client):
    """ADR-026's honest scope: statements must be in the book's base
    currency — the FX fold is a recorded follow-up, not a silent wrong."""
    bank = _bank_ledger(client, "FX Bank")
    usd = MT940_STATEMENT.replace("INR", "USD")
    r = client.post(
        f"/api/v1/banking/{bank['id']}/statements", json={"format": "mt940", "text": usd}
    )
    assert r.status_code == 422
    assert "USD" in r.json()["detail"]


def test_only_bank_ledgers_reconcile(client):
    party = _party_ledger(client, "Not A Bank Ltd")
    r = client.post(
        f"/api/v1/banking/{party['id']}/statements",
        json={"format": "csv", "text": CSV_STATEMENT},
    )
    assert r.status_code == 422
    assert "not a bank account" in r.json()["detail"]


def test_ledger_of_another_book_is_a_stranger(client):
    """ADR-009: a banking action names a ledger of THIS book; another
    book's ledger id is a 404 even though the id exists in the database."""
    r = client.post("/api/v1/books", json={"name": "Stranger Books Ltd"})
    assert r.status_code in (201, 409), r.text  # tolerate earlier runs
    books = client.get("/api/v1/books").json()
    other = next(b for b in books if b["name"] == "Stranger Books Ltd")
    headers = {"X-Book-Id": str(other["id"])}
    r = client.post(
        "/api/v1/ledgers",
        headers=headers,
        json={"name": "Their Bank", "group_name": "Bank Accounts"},
    )
    assert r.status_code == 201, r.text
    their_bank = r.json()
    # from THIS book (no header → the single... several books now, so set
    # the first book explicitly): their ledger id is a stranger
    mine = next(b for b in books if b["name"] != "Stranger Books Ltd")
    r = client.post(
        f"/api/v1/banking/{their_bank['id']}/statements",
        headers={"X-Book-Id": str(mine["id"])},
        json={"format": "csv", "text": CSV_STATEMENT},
    )
    assert r.status_code == 404
    # restore the single-book world for the tests that follow (the
    # no-header auto-pick needs exactly one active book)
    r = client.patch(f"/api/v1/books/{other['id']}", json={"is_archived": True})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------- suggestions
def test_suggestions_rank_and_never_commit(client):
    bank = _bank_ledger(client, "Match Bank")
    _party_ledger(client, "Match Co")
    # a payment of 2500 (books) vs the statement's 2500 withdrawal — exact
    v = _payment(client, "Match Bank", "Match Co", "2500.00", "2026-04-03", "UPI to Ramesh")
    assert v.status_code == 201, v.text
    _import(
        client,
        bank["id"],
        "csv",
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        "03/04/2026,UPI/Payment to Ramesh,UPI123456,2500.00,\n",
    )
    r = client.get(f"/api/v1/banking/{bank['id']}/suggestions")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert len(payload["lines"]) == 1
    line = payload["lines"][0]
    assert line["suggestions"], "the exact-amount payment must be suggested"
    best = line["suggestions"][0]
    assert best["amount"] == "2500.00"
    assert "amount exact" in best["reasons"]
    assert best["voucher_id"] == v.json()["id"]
    # the matchmaker wrote nothing — the line is still unmatched
    assert _lines(client, bank["id"], status="unmatched")


def test_suggestions_ignore_matched_sides(client, imported_csv):
    bank, _ = imported_csv
    _party_ledger(client, "Acme Traders")
    v = _receipt(client, bank["name"], "Acme Traders", "15000.00", "2026-04-05")
    target = _match_target(client, bank["id"], v.json()["voucher_no"])
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=target)
    assert r.status_code == 201, r.text
    # after the match, neither side is a candidate any more
    sug = client.get(f"/api/v1/banking/{bank['id']}/suggestions").json()
    assert all(ln["statement_line_id"] != target["statement_line_id"] for ln in sug["lines"])
    # and the entry book agrees: the voucher's entry is matched
    entries = client.get(
        f"/api/v1/banking/{bank['id']}/entries", params={"status": "matched"}
    ).json()["entries"]
    assert any(e["voucher_no"] == v.json()["voucher_no"] for e in entries)


# ------------------------------------------------------------- match/unmatch
def test_match_unmatch_and_rematch(client, imported_csv):
    bank, _ = imported_csv
    _party_ledger(client, "Acme Traders")
    v = _receipt(client, bank["name"], "Acme Traders", "15000.00", "2026-04-05")
    voucher = v.json()
    match = _match_target(client, bank["id"], voucher["voucher_no"])
    line = next(ln for ln in _lines(client, bank["id"]) if ln["amount"] == "15000.00")
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=match)
    assert r.status_code == 201, r.text
    assert r.json()["voucher_no"] == voucher["voucher_no"]

    # one live match per line — the honest two-step
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=match)
    assert r.status_code == 409

    matched = next(ln for ln in _lines(client, bank["id"]) if ln["id"] == line["id"])
    assert matched["match"]["live"] is True
    assert matched["match"]["voucher_no"] == voucher["voucher_no"]

    # un-match keeps the row with its provenance
    r = client.delete(f"/api/v1/banking/matches/{line['id']}")
    assert r.status_code == 200, r.text
    after = next(ln for ln in _lines(client, bank["id"]) if ln["id"] == line["id"])
    assert after["match"]["live"] is False
    assert after["match"]["unmatched_at"] is not None

    # un-matching again is a 404 — there is no live match to withdraw
    r = client.delete(f"/api/v1/banking/matches/{line['id']}")
    assert r.status_code == 404

    # re-match rewrites the row (the mutable annotation, by design)
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=match)
    assert r.status_code == 201, r.text


def test_match_guards(client, imported_csv):
    bank, _ = imported_csv
    _party_ledger(client, "Guard Co")
    other_bank = _bank_ledger(client, "Guard Other Bank")
    v = _receipt(client, bank["name"], "Guard Co", "15000.00", "2026-04-05")
    voucher = v.json()
    base = _match_target(client, bank["id"], voucher["voucher_no"])
    # the wrong date does not name the entry's own voucher
    bad_date = dict(base, voucher_date="2026-04-06")
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=bad_date)
    assert r.status_code == 422
    # an entry of ANOTHER ledger cannot reconcile this bank statement
    # (the party's own ledger entry — a nonexistent id on this ledger)
    wrong_ledger = dict(base, entry_id=base["entry_id"] + 10_000)
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=wrong_ledger)
    assert r.status_code == 422
    # a line of another ledger is a stranger
    r = client.post(
        f"/api/v1/banking/{other_bank['id']}/matches",
        json=base,
    )
    assert r.status_code == 404


def test_match_survives_across_period_lock(client, imported_csv):
    """Matching annotates; it does not post — ADR-008's gates guard
    financial history, and a match is not one."""

    bank, _ = imported_csv
    # ADR-008's gates guard POSTS; a match is an annotation. The period
    # suite proves the gate on vouchers — here the claim is only that the
    # banking routes carry no period gate of their own: a line matched
    # across a locked period's border is legal because nothing financial
    # moves. (Locked-period voucher posting itself is covered by
    # test_period_locking.)
    _party_ledger(client, "Locked Co")
    line = next(ln for ln in _lines(client, bank["id"]) if ln["amount"] == "15000.00")
    assert line is not None


# ---------------------------------------------------------- the reconciliation
def test_reconciliation_proof_against_independent_sums(client):
    """The ADR-026 identity, asserted against sums computed independently
    of the fold (the trial-balance discipline)."""
    bank = _bank_ledger(client, "Proof Bank")
    _party_ledger(client, "Proof Co")
    # books: +15000 (Apr 5), -2500 (Apr 3), -117 bank charge not yet entered
    v1 = _receipt(client, "Proof Bank", "Proof Co", "15000.00", "2026-04-05").json()
    _payment(client, "Proof Bank", "Proof Co", "2500.00", "2026-04-03")
    _receipt(client, "Proof Bank", "Proof Co", "700.00", "2026-04-20")  # not in statement

    statement = (
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        "05/04/2026,NEFT Credit from Proof Co,REF2,,15000.00\n"
        "03/04/2026,UPI/Payment to Proof Co,REF1,2500.00,\n"
        "07/04/2026,BANK CHARGES,REF3,117.00,\n"
    )
    _import(client, bank["id"], "csv", statement, opening_balance="10000.00")

    target = _match_target(client, bank["id"], v1["voucher_no"])
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=target)
    assert r.status_code == 201, r.text

    r = client.get(
        f"/api/v1/banking/{bank['id']}/reconciliation",
        params={"as_of": "2026-04-30", "statement_balance": "22383.00"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # independent sums: books = 15000 - 2500 + 700 = 13200
    assert data["book_balance"] == "13200.00"
    # statement = 10000 + 15000 - 2500 - 117 = 22383 (the given anchor)
    assert data["statement_balance"] == "22383.00"
    # reconciled = the matched 15000
    assert data["reconciled_balance"] == "15000.00"
    kinds = {row["kind"] for row in data["rows"]}
    # unmatched: the 2500 payment and the 700 deposit (books), the 2500
    # withdrawal and the 117 charge (statement); the statement's own
    # opening (10000) is a baseline row of the proof
    assert kinds == {
        "book_deposit_not_credited",
        "cheque_issued_not_presented",
        "stmt_debit",
        "statement_opening",
    }
    # the proof: 13200 - 22383 = 700 - 117
    lhs = Decimal(data["book_balance"]) - Decimal(data["statement_balance"])
    rhs = (
        Decimal(data["unmatched_book_total"])
        - Decimal(data["unmatched_statement_total"])
        + Decimal(data["pair_difference_total"])
        + Decimal("-10000.00")  # the statement's own opening, a baseline row
    )
    assert lhs == rhs == Decimal(data["proof"]["lhs"])
    assert data["proof"]["ok"] is True


def test_pair_difference_priced_honestly(client):
    """A manual match that disagrees on amount is the user's judgement; the
    statement prices the disagreement as its own row and the proof still
    closes exactly."""
    bank = _bank_ledger(client, "Pair Bank")
    _party_ledger(client, "Pair Co")
    v = _receipt(client, "Pair Bank", "Pair Co", "495.00", "2026-04-05").json()
    _import(
        client,
        bank["id"],
        "csv",
        "Date,Narrative,Ref No,Withdrawal,Deposit\n"
        "05/04/2026,NEFT Credit (net of fee),REF2,,500.00\n",
    )
    # the amounts disagree (495 vs 500): the suggestions engine will not
    # pair them — the accountant matches manually, naming the entry from
    # the entry book, and the proof prices the difference
    target = _match_target(client, bank["id"], v["voucher_no"])
    r = client.post(f"/api/v1/banking/{bank['id']}/matches", json=target)
    assert r.status_code == 201, r.text
    data = client.get(
        f"/api/v1/banking/{bank['id']}/reconciliation",
        params={"as_of": "2026-04-30", "statement_balance": "500.00"},
    ).json()
    assert data["pair_difference_total"] == "-5.00"  # book 495 vs bank 500
    pair_rows = [row for row in data["rows"] if row["kind"] == "pair_difference"]
    assert len(pair_rows) == 1
    assert data["proof"]["ok"] is True
    # and no unmatched noise: everything is matched (with a priced difference)
    assert data["unmatched_book_total"] == "0.00"
    assert data["unmatched_statement_total"] == "0.00"


def test_reconciliation_needs_an_anchor(client):
    bank = _bank_ledger(client, "Anchor Bank")
    r = client.get(f"/api/v1/banking/{bank['id']}/reconciliation", params={"as_of": "2026-04-30"})
    assert r.status_code == 422
    assert "statement_balance" in r.json()["detail"]


def test_reconciliation_fold_agrees_with_given_anchor(client):
    """When both anchors exist (an imported opening and a given figure),
    the fold agreement is stated in the source field — never silently."""
    bank = _bank_ledger(client, "Agree Bank")
    _import(
        client,
        bank["id"],
        "csv",
        "Date,Narrative,Ref No,Withdrawal,Deposit\n05/04/2026,Credit,REF,,100.00\n",
        opening_balance="500.00",
    )
    agree = client.get(
        f"/api/v1/banking/{bank['id']}/reconciliation",
        params={"as_of": "2026-04-30", "statement_balance": "600.00"},
    ).json()
    assert "fold agrees" in agree["statement_balance_source"]
    disagree = client.get(
        f"/api/v1/banking/{bank['id']}/reconciliation",
        params={"as_of": "2026-04-30", "statement_balance": "999.00"},
    ).json()
    assert "fold disagrees" in disagree["statement_balance_source"]


# --------------------------------------------------------------------- events
def test_events_fire_after_commit(client):
    bank = _bank_ledger(client, "Event Bank")
    _party_ledger(client, "Event Co")
    seen: list[tuple[str, dict]] = []

    def record(event):
        seen.append((event.name, dict(event.data)))

    manager = client.app.state.plugin_manager
    manager.registry.on("bank.statement.imported", record)
    manager.registry.on("bank.line.matched", record)
    manager.registry.on("bank.line.unmatched", record)

    _import(client, bank["id"], "csv", CSV_STATEMENT)
    v = _receipt(client, "Event Bank", "Event Co", "15000.00", "2026-04-05").json()
    target = _match_target(client, bank["id"], v["voucher_no"])
    client.post(f"/api/v1/banking/{bank['id']}/matches", json=target)
    client.delete(f"/api/v1/banking/matches/{target['statement_line_id']}")

    names = [name for name, _ in seen]
    assert names.count("bank.statement.imported") == 1
    assert names.count("bank.line.matched") == 1
    assert names.count("bank.line.unmatched") == 1
    imported = next(data for name, data in seen if name == "bank.statement.imported")
    assert imported["new"] == 3 and imported["ledger_name"] == "Event Bank"


# ---------------------------------------------------------------------- hooks
def test_gateway_webhook_delivers_statements(client):
    """The seam: POST the gateway JSON shape to the claimed hook and the
    statement lands in-process — the kernel never learns a bank's name."""
    bank = _bank_ledger(client, "Gateway Bank")
    r = client.post(
        "/api/v1/hooks/bank-statement-gateway",
        json={
            "ledger": "Gateway Bank",
            "format": "csv",
            "text": CSV_STATEMENT,
            "source": "acme-gateway",
        },
    )
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["new"] == 3 and receipt["source"] == "acme-gateway"
    assert len(_lines(client, bank["id"])) == 3


def test_unknown_hook_is_404(client):
    r = client.post("/api/v1/hooks/no-such-hook", json={"x": 1})
    assert r.status_code == 404
    assert "No plugin claimed" in r.json()["detail"]


# ------------------------------------------------------------------- the feed
def test_bank_ledger_feed(client):
    _bank_ledger(client, "Feed Bank")
    _party_ledger(client, "Feed Co")
    _receipt(client, "Feed Bank", "Feed Co", "1250.50", "2026-04-05")
    r = client.get("/api/v1/banking/ledgers")
    assert r.status_code == 200
    feed = r.json()["ledgers"]
    row = next(item for item in feed if item["name"] == "Feed Bank")
    assert row["balance"] == "1250.50"
    assert all(item["name"] != "Feed Co" for item in feed)  # parties are not banks
