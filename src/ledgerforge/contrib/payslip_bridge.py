"""The payslip distribution bridge — the out-of-process half (ADR-029).

The payroll pack's after-commit handler appends one
``lf-payroll-payslip/1`` envelope per employee of a posted run to the
``LF_PAYROLL_OUTBOX`` JSONL file; **this** program turns those envelopes
into delivered payslips. It runs as its own process (``lf-payslip-bridge``
or ``python -m ledgerforge.contrib.payslip_bridge``), entirely outside the
posting path — the latency-tolerant integration side of the plugin
contract (D3, the IRN/EWB precedent applied to distribution): the run
posted inside the transaction where Law 1 lives, the rendering and the
mail run here where a slow or down engine or SMTP server can never slow a
posting. **Core never sends mail; this bridge is the only mailer, and it
mails only what the operator configured.**

What it does, per pending outbox line:

- Fetch the payslip PDF from the engine:
  ``GET {base}/api/v1/vouchers/{voucher_id}/pdf?employee=<name>`` — the
  same ADR-013 layout engine the Ctrl+P path renders through, so a
  delivered payslip is byte-identical to a printed one. ``X-Book-Id``
  rides the request (books are strangers), ``LF_PAYROLL_TOKEN`` is the
  optional bearer credential (personal API token).
- Write the batch under ``--out`` (default ``payslips/``), one PDF per
  employee per period, named from the view's own filename hint.
- When the run entry carried an ``email`` **and** the operator configured
  SMTP (``LF_PAYROLL_SMTP_HOST``, optionally ``_PORT``/``_USER``/``_PASS``/
  ``_FROM``/``_TLS``), email each PDF as an attachment. No email on the
  entry → PDF only, never invented (a blank address is never guessed).
- **2xx** — record the delivery (path, emailed or not) in the responses
  file and remove the line from the outbox.
- **4xx** — the request is bad (unknown voucher, wrong book, no payslip
  for that employee): park the line in ``<outbox>.failed.jsonl`` with the
  error, for an operator to fix. Retrying a rejected request unchanged is
  not delivery, it is spamming.
- **5xx / network error** — leave the line pending; the next pass retries.
  Eventual consistency, on purpose.

Files (defaults alongside the outbox): ``payslip-responses.jsonl`` (one
JSON line per delivered payslip) and ``<outbox>.failed.jsonl`` (parked
4xx lines, with ``error``). Outbox rewrites are atomic (temp file +
rename), so a crash mid-ship can lose nothing: a shipped payslip is
either still in the outbox (retried; the file name is deterministic) or
in the responses file.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import smtplib
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger("ledgerforge.payslip_bridge")

_TIMEOUT = 20  # seconds per engine call
_USER_AGENT = "ledgerforge-payslip-bridge/1"
ENVELOPE_KIND = "lf-payroll-payslip/1"


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            # a torn line (crash mid-write) parks as failed — never dropped
            lines.append({"kind": "unparseable", "line": raw, "error": "invalid JSON"})
    return lines


def _write_lines(path: Path, lines: list[dict]) -> None:
    """Atomic replace: write a sibling temp file, rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _append_line(path: Path, line: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _safe_stem(name: str) -> str:
    """A file name made download-safe (printing.py's rule)."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(". ")
    return cleaned[:100] or "payslip"


def fetch_payslip(base_url: str, envelope: dict, token: str | None) -> tuple[int, bytes | str]:
    """One engine round-trip: ``(status, pdf-bytes-or-error-text)``.
    Network errors surface as ``(0, text)`` — pending, like a 5xx."""
    employee = urllib.parse.quote(str(envelope.get("employee") or ""))
    url = (
        f"{base_url.rstrip('/')}/api/v1/vouchers/{envelope.get('voucher_id')}"
        f"/pdf?employee={employee}"
    )
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf"}
    book_id = envelope.get("book_id")
    if book_id is not None:
        headers["X-Book-Id"] = str(book_id)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"network error: {exc}"


class SmtpConfig:
    """The operator's mail facts, from the ``LF_PAYROLL_SMTP_*`` env —
    blank host means PDF-only (the bridge never invents a server)."""

    def __init__(self) -> None:
        self.host = os.environ.get("LF_PAYROLL_SMTP_HOST", "").strip()
        self.port = int(os.environ.get("LF_PAYROLL_SMTP_PORT", "587") or 587)
        self.user = os.environ.get("LF_PAYROLL_SMTP_USER", "").strip()
        self.password = os.environ.get("LF_PAYROLL_SMTP_PASS", "")
        self.sender = os.environ.get("LF_PAYROLL_SMTP_FROM", "").strip() or self.user
        self.tls = os.environ.get("LF_PAYROLL_SMTP_TLS", "1").strip() not in ("", "0", "false")

    @property
    def configured(self) -> bool:
        return bool(self.host)


def send_payslip_mail(config: SmtpConfig, envelope: dict, pdf: bytes, filename: str) -> None:
    """One payslip, one mail — attachment only, no tracking, no read
    receipt. Raises on failure (the caller parks the line as pending)."""
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = str(envelope["email"])
    message["Subject"] = f"Payslip {envelope.get('period')} — {envelope.get('employee')}"
    message.set_content(
        f"Dear {envelope.get('employee')},\n\n"
        f"Your payslip for {envelope.get('period')} is attached.\n\n"
        "This message was sent by the operator's payslip bridge (ADR-029)."
    )
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )
    with smtplib.SMTP(config.host, config.port, timeout=_TIMEOUT) as server:
        if config.tls:
            server.starttls(context=ssl.create_default_context())
        if config.user:
            server.login(config.user, config.password)
        server.send_message(message)


def process_pending(
    outbox: Path,
    base_url: str,
    out_dir: Path,
    responses: Path,
    *,
    token: str | None = None,
    smtp: SmtpConfig | None = None,
) -> dict:
    """One delivery pass. Returns counters (``delivered``/``emailed``/
    ``failed``/``pending``) — the CLI's exit status and the log's story."""
    pending = _read_lines(outbox)
    stats = {"delivered": 0, "emailed": 0, "failed": 0, "pending": 0}
    if not pending:
        return stats
    smtp = smtp if smtp is not None else SmtpConfig()

    still_pending: list[dict] = []
    for line in pending:
        if line.get("kind") == "unparseable" or line.get("kind") != ENVELOPE_KIND:
            failed = dict(line)
            failed.setdefault("error", "not a lf-payroll-payslip/1 envelope")
            _append_line(outbox.with_name(outbox.name + ".failed.jsonl"), failed)
            stats["failed"] += 1
            continue

        status, body = fetch_payslip(base_url, line, token)
        if not (200 <= status < 300) or not isinstance(body, bytes):
            if 400 <= status < 500:
                failed = dict(line)
                failed["error"] = body if isinstance(body, str) else str(body)
                failed["status"] = status
                _append_line(outbox.with_name(outbox.name + ".failed.jsonl"), failed)
                stats["failed"] += 1
                logger.error("engine rejected %s (%s)", line.get("voucher_no"), status)
            else:
                still_pending.append(line)
                stats["pending"] += 1
                logger.warning(
                    "engine unavailable (%s): %s/%s stays pending",
                    status or "network",
                    line.get("voucher_no"),
                    line.get("employee"),
                )
            continue

        stem = _safe_stem(f"Payslip {line.get('employee')} {line.get('period')}")
        target = out_dir / f"{stem}.pdf"
        out_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

        emailed = False
        if line.get("email") and smtp.configured:
            try:
                send_payslip_mail(smtp, line, body, target.name)
                emailed = True
            except (smtplib.SMTPException, OSError) as exc:
                # the PDF is delivered to disk; the mail stays pending —
                # the whole line retries (the file name is deterministic)
                line["error"] = f"smtp error: {exc}"
                still_pending.append(line)
                stats["pending"] += 1
                logger.warning("smtp unavailable: %s stays pending (%s)", stem, exc)
                continue

        _append_line(
            responses,
            {
                "voucher_no": line.get("voucher_no"),
                "voucher_id": line.get("voucher_id"),
                "book_id": line.get("book_id"),
                "employee": line.get("employee"),
                "period": line.get("period"),
                "path": str(target),
                "emailed": emailed,
            },
        )
        stats["delivered"] += 1
        stats["emailed"] += 1 if emailed else 0
        logger.info("payslip delivered: %s%s", target, " (emailed)" if emailed else "")
    _write_lines(outbox, still_pending)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lf-payslip-bridge",
        description=(
            "Deliver the payslip outbox (out-of-process; ADR-029): render each "
            "envelope's PDF through the engine, write the batch under --out, and "
            "email when the entry carried an address and SMTP is configured. "
            "Core never sends mail; this bridge is the only mailer."
        ),
    )
    parser.add_argument(
        "--outbox",
        default=os.environ.get("LF_PAYROLL_OUTBOX", ""),
        help="the JSONL outbox (default: LF_PAYROLL_OUTBOX)",
    )
    parser.add_argument(
        "--engine",
        default=os.environ.get("LF_PAYROLL_ENGINE", "http://127.0.0.1:8000"),
        help="the engine's base URL (default: LF_PAYROLL_ENGINE or http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LF_PAYROLL_TOKEN", ""),
        help="a personal API token when the engine requires auth (default: LF_PAYROLL_TOKEN)",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("LF_PAYROLL_OUT_DIR", "payslips"),
        help="the PDF batch directory (default: LF_PAYROLL_OUT_DIR or ./payslips)",
    )
    parser.add_argument(
        "--responses",
        default="",
        help="the responses JSONL (default: <outbox dir>/payslip-responses.jsonl)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="deliver the pending lines once and exit (default: poll forever)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30.0,
        help="poll interval when running continuously (default: 30)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not args.outbox:
        parser.error("the outbox is required (--outbox or LF_PAYROLL_OUTBOX)")

    outbox = Path(args.outbox)
    responses = (
        Path(args.responses) if args.responses else outbox.with_name("payslip-responses.jsonl")
    )
    out_dir = Path(args.out)

    while True:
        stats = process_pending(outbox, args.engine, out_dir, responses, token=args.token)
        if args.once:
            return 0 if stats["failed"] == 0 else 1
        import time

        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
