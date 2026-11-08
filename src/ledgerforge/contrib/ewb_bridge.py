"""The e-way-bill outbox bridge — the out-of-process half (ADR-028).

The GST pack's after-commit handler appends every threshold-crossing
document's movement envelope to the ``LF_GST_EWB_OUTBOX`` JSONL file;
**this** program ships them to the operator's NIC/ASP adapter. It runs
as its own process (``lf-ewb-bridge`` or ``python -m
ledgerforge.contrib.ewb_bridge``), entirely outside the posting path —
the latency-tolerant integration side of the plugin contract (D3, the
IRN precedent applied to movement): the tax ran inside the posting
transaction where Law 1 lives, the NIC/ASP round-trip runs here where a
slow or down adapter can never slow a posting.

What it does, per pending outbox line:

- POST the envelope (JSON, ``kind: lf-gst-ewaybill/1``) to the operator's
  NIC/ASP adapter — the translator that talks to the actual e-way-bill
  portal. The adapter answers with at least ``ewb_no``, and typically the
  validity window (``ewb_date``/``valid_upto``).
- **2xx** — record the response (echoed fields verbatim) in the responses
  file and remove the line from the outbox.
- **4xx** — the payload is bad: park the line in ``<outbox>.failed.jsonl``
  with the adapter's error, for an operator to fix. Retrying a rejected
  payload unchanged is not shipping, it is spamming.
- **5xx / network error** — leave the line pending; the next pass retries.
  Eventual consistency, on purpose.

Files (defaults alongside the outbox): ``ewb-responses.jsonl`` (one JSON
line per generated e-way bill) and ``<outbox>.failed.jsonl`` (parked 4xx
lines, with ``error``). All writes are atomic (temp file + rename), so a
crash mid-ship can lose nothing: a shipped line is either still in the
outbox (retried; adapters deduplicate by document number) or in responses.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("ledgerforge.ewb_bridge")

_TIMEOUT = 15  # seconds per NIC/ASP adapter call
_USER_AGENT = "ledgerforge-ewb-bridge/1"


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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def post_envelope(endpoint: str, envelope: dict) -> tuple[int, dict | str]:
    """One adapter round-trip: returns ``(status, body)`` — the parsed JSON
    body on 2xx, the error text otherwise. Network errors surface as
    ``(0, text)`` — pending, like a 5xx."""
    payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — the endpoint is the operator's
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"network error: {exc}"
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


def process_pending(outbox: Path, endpoint: str, responses: Path) -> dict:
    """One shipping pass. Returns counters (``shipped``/``failed``/
    ``pending``/``invalid``) — the CLI's exit status and the log's story."""
    pending = _read_lines(outbox)
    stats = {"shipped": 0, "failed": 0, "pending": 0, "invalid": 0}
    if not pending:
        return stats

    still_pending: list[dict] = []
    for line in pending:
        if line.get("kind") == "unparseable" or not isinstance(line, dict):
            line.setdefault("error", "invalid outbox line")
            _append_line(outbox.with_name(outbox.name + ".failed.jsonl"), line)
            stats["invalid"] += 1
            continue
        status, body = post_envelope(endpoint, line)
        if 200 <= status < 300 and isinstance(body, dict):
            record = {
                "voucher_no": line.get("voucher_no"),
                "voucher_id": line.get("voucher_id"),
                "book_id": line.get("book_id"),
                "endpoint": endpoint,
                "status": status,
            }
            # the adapter's answer, echoed verbatim (ewb_no / ewb_date / valid_upto)
            for key in ("ewb_no", "ewb_date", "valid_upto", "cancel_status", "error_code"):
                if key in body:
                    record[key] = body[key]
            _append_line(responses, record)
            stats["shipped"] += 1
            logger.info("e-way bill generated for %s", line.get("voucher_no"))
        elif status >= 400 and status < 500:
            failed = dict(line)
            failed["error"] = body if isinstance(body, str) else json.dumps(body)
            failed["status"] = status
            _append_line(outbox.with_name(outbox.name + ".failed.jsonl"), failed)
            stats["failed"] += 1
            logger.error("adapter rejected %s (%s)", line.get("voucher_no"), status)
        else:
            still_pending.append(line)
            stats["pending"] += 1
            logger.warning(
                "adapter unavailable (%s): %s stays pending",
                status or "network",
                line.get("voucher_no"),
            )
    _write_lines(outbox, still_pending)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lf-ewb-bridge",
        description=(
            "Ship the GST e-way-bill outbox to the NIC/ASP adapter (out-of-process; "
            "ADR-028). 2xx lines move to the responses file, 4xx lines park in "
            "<outbox>.failed.jsonl, 5xx/network lines stay pending."
        ),
    )
    parser.add_argument(
        "--outbox",
        default=os.environ.get("LF_GST_EWB_OUTBOX", ""),
        help="the JSONL outbox (default: LF_GST_EWB_OUTBOX)",
    )
    parser.add_argument("--endpoint", required=True, help="the NIC/ASP adapter's URL")
    parser.add_argument(
        "--responses",
        default="",
        help="the responses JSONL (default: <outbox dir>/ewb-responses.jsonl)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="ship the pending lines once and exit (default: poll forever)",
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
        parser.error("no outbox given: pass --outbox or set LF_GST_EWB_OUTBOX")
    outbox = Path(args.outbox)
    responses = Path(args.responses) if args.responses else outbox.with_name("ewb-responses.jsonl")

    while True:
        stats = process_pending(outbox, args.endpoint, responses)
        if args.once:
            print(json.dumps(stats))  # machine-readable for scripts/CI
            return 0 if stats["failed"] == 0 else 1
        if any(stats.values()):
            logger.info("pass complete: %s", stats)
        time.sleep(max(args.poll_seconds, 0.5))


if __name__ == "__main__":
    sys.exit(main())
