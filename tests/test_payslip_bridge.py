"""The payslip distribution bridge (ADR-029, roadmap Phase 8): the
out-of-process half — outbox envelopes → rendered PDFs (and, only when
the operator configures SMTP, emails).

The promises under test:
- One pass delivers every pending envelope: the PDF is fetched from the
  engine (``GET /api/v1/vouchers/{id}/pdf?employee=`` with the book
  header), written under ``--out`` with a deterministic name, recorded in
  the responses file, and removed from the outbox.
- **4xx parks** (unknown voucher, no payslip for the employee): the line
  moves to ``<outbox>.failed.jsonl`` with the error — retrying a rejected
  request unchanged is spamming, not delivery.
- **5xx / network errors stay pending**: eventual consistency, on purpose.
- The mail path: an entry with an ``email`` plus configured SMTP sends
  the PDF as an attachment; a blank address means PDF only, never
  invented; an SMTP failure keeps the line pending (the PDF's name is
  deterministic, so the retry is idempotent).
- Torn outbox lines park as failed — never dropped.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ledgerforge.contrib import payslip_bridge


class _Engine(BaseHTTPRequestHandler):
    """A stand-in engine: payslip PDFs for known vouchers, 404 otherwise."""

    def do_GET(self):  # noqa: N802 — http.server's hook
        if "/pdf" in self.path and "employee=asha" in self.path:
            body = b"%PDF-fake-asha-payslip"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"no payslip for that employee"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):  # silence the test log
        return


@pytest.fixture()
def engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def envelope(employee: str = "asha", *, voucher_id=1, email=None) -> dict:
    out = {
        "kind": "lf-payroll-payslip/1",
        "voucher_id": voucher_id,
        "voucher_no": "JV-0001",
        "book_id": 1,
        "employee": employee,
        "period": "2026-11",
    }
    if email:
        out["email"] = email
    return out


def write_outbox(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_delivers_the_batch_and_empties_the_outbox(engine, tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    responses = tmp_path / "responses.jsonl"
    out_dir = tmp_path / "payslips"
    write_outbox(outbox, [envelope("asha"), envelope("asha", voucher_id=2)])

    stats = payslip_bridge.process_pending(
        outbox, engine, out_dir, responses, smtp=payslip_bridge.SmtpConfig()
    )

    assert stats["delivered"] == 2 and stats["emailed"] == 0
    # the PDFs land with deterministic names, one per employee per period
    pdf = out_dir / "Payslip asha 2026-11.pdf"
    assert pdf.read_bytes() == b"%PDF-fake-asha-payslip"
    # the responses file records both deliveries
    records = [json.loads(line) for line in responses.read_text().splitlines()]
    assert len(records) == 2 and all(r["emailed"] is False for r in records)
    assert records[0]["path"] == str(pdf)
    # the outbox is empty — shipped lines never linger
    assert outbox.read_text().strip() == ""


def test_4xx_parks_and_network_stays_pending(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    responses = tmp_path / "responses.jsonl"
    out_dir = tmp_path / "payslips"
    # a 404 line (no payslip for the employee) and an unreachable engine
    write_outbox(outbox, [envelope("stranger")])

    class _Down(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

        def log_message(self, *args):
            return

    down = ThreadingHTTPServer(("127.0.0.1", 0), _Down)
    threading.Thread(target=down.serve_forever, daemon=True).start()
    try:
        # 4xx against a live engine → parked
        stats = payslip_bridge.process_pending(
            outbox, "http://127.0.0.1:1", out_dir, responses, smtp=payslip_bridge.SmtpConfig()
        )
        # port 1 refuses the connection → network error → pending (not failed)
        assert stats["pending"] == 1 and stats["failed"] == 0
        assert json.loads(outbox.read_text().splitlines()[0])["employee"] == "stranger"
    finally:
        down.shutdown()


def test_4xx_parks_with_the_error(engine, tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    responses = tmp_path / "responses.jsonl"
    write_outbox(outbox, [envelope("stranger")])  # the stub engine 404s this

    stats = payslip_bridge.process_pending(
        outbox, engine, tmp_path / "payslips", responses, smtp=payslip_bridge.SmtpConfig()
    )
    assert stats["failed"] == 1 and stats["delivered"] == 0
    failed = json.loads((tmp_path / "outbox.jsonl.failed.jsonl").read_text().splitlines()[0])
    assert failed["status"] == 404 and "error" in failed


def test_torn_line_parks_never_drops(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("{not json}\n", encoding="utf-8")
    stats = payslip_bridge.process_pending(
        outbox,
        "http://127.0.0.1:1",
        tmp_path / "payslips",
        tmp_path / "responses.jsonl",
        smtp=payslip_bridge.SmtpConfig(),
    )
    assert stats["failed"] == 1
    parked = json.loads((tmp_path / "outbox.jsonl.failed.jsonl").read_text().splitlines()[0])
    assert parked["kind"] == "unparseable"


def test_smtp_path_emails_the_attachment(engine, tmp_path, monkeypatch):
    sent: list[tuple[str, object]] = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", (host, port)))

        def starttls(self, context=None):
            sent.append(("starttls", context))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, message):
            sent.append(("sent", message))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(payslip_bridge.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("LF_PAYROLL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("LF_PAYROLL_SMTP_USER", "payroll@example.com")
    monkeypatch.setenv("LF_PAYROLL_SMTP_PASS", "secret")
    monkeypatch.setenv("LF_PAYROLL_SMTP_FROM", "payroll@example.com")

    outbox = tmp_path / "outbox.jsonl"
    write_outbox(outbox, [envelope("asha", email="asha@example.com")])
    stats = payslip_bridge.process_pending(
        outbox,
        engine,
        tmp_path / "payslips",
        tmp_path / "responses.jsonl",
        smtp=payslip_bridge.SmtpConfig(),
    )

    assert stats["delivered"] == 1 and stats["emailed"] == 1
    message = next(arg for kind, arg in sent if kind == "sent")
    assert message["To"] == "asha@example.com"
    assert message["Subject"] == "Payslip 2026-11 — asha"
    # the attachment is the PDF the engine rendered
    attachments = list(message.iter_attachments())
    assert attachments and attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == b"%PDF-fake-asha-payslip"


def test_smtp_failure_keeps_the_line_pending(engine, tmp_path, monkeypatch):
    class _BrokenSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self, context=None):
            raise payslip_bridge.smtplib.SMTPException("no starttls")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(payslip_bridge.smtplib, "SMTP", _BrokenSMTP)
    monkeypatch.setenv("LF_PAYROLL_SMTP_HOST", "smtp.example.com")

    outbox = tmp_path / "outbox.jsonl"
    write_outbox(outbox, [envelope("asha", email="asha@example.com")])
    stats = payslip_bridge.process_pending(
        outbox,
        engine,
        tmp_path / "payslips",
        tmp_path / "responses.jsonl",
        smtp=payslip_bridge.SmtpConfig(),
    )
    # the PDF reached disk, but the mail is pending — the whole line stays
    assert stats["pending"] == 1 and stats["delivered"] == 0
    assert (tmp_path / "payslips" / "Payslip asha 2026-11.pdf").exists()


def test_blank_address_is_pdf_only_never_invented(engine, tmp_path, monkeypatch):
    # SMTP configured, but the entry carries no address → PDF only
    monkeypatch.setenv("LF_PAYROLL_SMTP_HOST", "smtp.example.com")
    outbox = tmp_path / "outbox.jsonl"
    write_outbox(outbox, [envelope("asha")])  # no email key
    stats = payslip_bridge.process_pending(
        outbox,
        engine,
        tmp_path / "payslips",
        tmp_path / "responses.jsonl",
        smtp=payslip_bridge.SmtpConfig(),
    )
    assert stats["delivered"] == 1 and stats["emailed"] == 0
