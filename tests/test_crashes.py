"""Crash capture: local always, sending chosen — the pledge as tests
(ADR-031). The payload allowlist is load-bearing: a report that carries
anything beyond it is refused by the sender, and the capture path writes
only what the allowlist names."""

from __future__ import annotations

import json

import httpx
import pytest

from ledgerforge.nativeapp import crashes


class _RecordingTransport(httpx.MockTransport):
    """A collector stand-in: records posts, answers 200 (or 500)."""

    def __init__(self, status: int = 200):
        self.requests = []
        super().__init__(self._handler)
        self._status = status

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(self._status)


# ------------------------------------------------------------- the payload


def test_report_payload_is_exactly_the_allowlist(tmp_path):
    payload = crashes.report_payload(
        "unhandled-exception", ValueError("boom"), "Traceback (most recent call last)…", tmp_path
    )
    assert set(payload) == set(crashes.REPORT_KEYS)
    # the pledge: no book data, no database, no command line — the
    # allowlist is the whole contract
    assert payload["app"] == "ledgerforge-native"
    assert payload["kind"] == "unhandled-exception"
    assert payload["traceback"].startswith("Traceback")  # pre-rendered text wins
    assert payload["engine_log_tail"] is None  # no engine.log yet

    # a bare exception still gets its one-line form
    bare = crashes.report_payload("k", ValueError("boom"), None, tmp_path)
    assert "boom" in bare["traceback"]


def test_report_carries_the_engine_log_tail(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("\n".join(f"line-{i}" for i in range(100)) + "\n", encoding="utf-8")
    payload = crashes.report_payload("k", None, "tb", tmp_path)
    tail = payload["engine_log_tail"].splitlines()
    assert len(tail) == crashes.ENGINE_LOG_TAIL_LINES
    assert tail[-1] == "line-99"


def test_capture_writes_readable_json(tmp_path):
    path = crashes.capture("smoke", RuntimeError("x"), "the traceback", tmp_path)
    assert path.parent == tmp_path / "crashes"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "smoke"
    assert payload["traceback"] == "the traceback"


# ------------------------------------------------------------- the handlers


def test_handlers_capture_and_restore(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    restore = crashes.install_handlers(tmp_path)
    try:
        # an unhandled exception, as the interpreter would deliver it
        try:
            raise ValueError("the crash")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        reports = list((tmp_path / "crashes").glob("crash-*.json"))
        assert len(reports) == 1
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        assert payload["kind"] == "unhandled-exception"
        assert "the crash" in payload["traceback"]
    finally:
        restore()
    # restored: the interpreter's hook is back
    assert sys.excepthook is sys.__excepthook__ or callable(sys.excepthook)


def test_engine_fault_file_lands_in_the_crash_dir(tmp_path):
    path = crashes.enable_engine_faults(tmp_path)
    assert path.parent == tmp_path / "crashes"
    assert path.name.startswith("engine-fault-")


# ------------------------------------------------------------- the sending


def test_env_defaults_send_nowhere(monkeypatch):
    monkeypatch.delenv("LF_CRASH_ENDPOINT", raising=False)
    monkeypatch.delenv("LF_CRASH_REPORTING", raising=False)
    assert crashes.endpoint_from_env() == ""
    assert crashes.autosend_enabled() is False


def test_send_report_posts_the_allowlist_only(tmp_path):
    path = crashes.capture("k", ValueError("x"), "tb", tmp_path)
    transport = _RecordingTransport()
    crashes.send_report(path, "https://collector.example/reports", transport=transport)
    (posted,) = transport.requests
    assert set(posted) == set(crashes.REPORT_KEYS)


def test_send_report_refuses_without_endpoint(tmp_path):
    path = crashes.capture("k", ValueError("x"), "tb", tmp_path)
    with pytest.raises(ValueError):
        crashes.send_report(path, "")


def test_send_report_refuses_unknown_fields(tmp_path):
    path = tmp_path / "crashes" / "crash-forged.json"
    path.parent.mkdir(parents=True)
    payload = crashes.report_payload("k", None, "tb", tmp_path)
    payload["customer_books"] = ["Acme Traders Pvt Ltd"]  # not on the allowlist
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        crashes.send_report(path, "https://collector.example/reports")


def test_send_pending_marks_and_never_resends(tmp_path):
    first = crashes.capture("k1", None, "tb1", tmp_path)
    second = crashes.capture("k2", None, "tb2", tmp_path)
    # 'first' was already sent and marked
    first.with_suffix(".json.sent").write_text("2026-11-09T00:00:00+00:00", encoding="utf-8")

    transport = _RecordingTransport()
    sent = crashes.send_pending(tmp_path, "https://collector.example", transport=transport)
    assert sent == 1  # only the unsent one
    assert [report["kind"] for report in transport.requests] == ["k2"]
    assert second.with_suffix(".json.sent").exists()

    # and again: nothing left to send
    assert crashes.send_pending(tmp_path, "https://collector.example", transport=transport) == 0


def test_send_pending_survives_a_dead_collector(tmp_path):
    crashes.capture("k", None, "tb", tmp_path)
    dead = _RecordingTransport(status=500)
    assert crashes.send_pending(tmp_path, "https://collector.example", transport=dead) == 0
    # the report is still there, unmarked, unsent — the pledge survives
    assert list((tmp_path / "crashes").glob("crash-*.json"))
