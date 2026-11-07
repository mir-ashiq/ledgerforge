"""The loopback API client — the Qt shell's only door into the engine.

Synchronous ``httpx`` against ``http://127.0.0.1:<port>``; calls run on worker
threads (``tasks.run``) so the GUI never blocks. Errors are normalised into
``ApiError`` carrying the engine's own ``detail`` text, so a voucher that
fails Law 1 shows the server's words, not an HTTP traceback.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from .models import Book, Group, Ledger, Period, Slot, TrialBalanceRow, Voucher


class ApiError(RuntimeError):
    """An engine response outside 2xx — ``detail`` is the server's message."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _detail(payload: Any, fallback: str) -> str:
    """Pull FastAPI's ``detail`` out of an error body (str or 422 list)."""
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, list):  # pydantic validation errors
        parts = []
        for item in detail:
            if isinstance(item, dict):
                where = ".".join(str(p) for p in item.get("loc", ()) if p != "body")
                msg = str(item.get("msg", "invalid"))
                parts.append(f"{where}: {msg}" if where else msg)
            else:
                parts.append(str(item))
        return "; ".join(parts) or fallback
    if detail is None or str(detail) == "":
        return fallback
    return str(detail)


class ApiClient:
    """One book-scoped client; ``X-Book-Id`` rides every call (ADR-009)."""

    def __init__(self, port: int, *, book_id: int | None = None, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=timeout,
            headers={"X-Book-Id": str(book_id)} if book_id is not None else {},
        )

    # ------------------------------------------------------------- plumbing
    @property
    def book_id(self) -> int | None:
        raw = self._client.headers.get("X-Book-Id")
        return int(raw) if raw is not None else None

    def set_book(self, book_id: int) -> None:
        self._client.headers["X-Book-Id"] = str(book_id)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        response = self._client.get(path, params=clean)
        return self._unwrap(response)

    def _post(self, path: str, payload: dict | None = None, **params: Any) -> Any:
        response = self._client.post(path, json=payload or {}, params=params)
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise ApiError(response.status_code, _detail(body, response.text[:300]))
        if not response.content:
            return None
        return response.json()

    # ----------------------------------------------------------------- meta
    def health(self) -> dict:
        return self._get("/health")

    # ---------------------------------------------------------------- books
    def books(self) -> list[Book]:
        return [Book.from_json(r) for r in self._get("/api/v1/books")]

    # -------------------------------------------------------------- masters
    def ledgers(self, search: str = "") -> list[Ledger]:
        return [Ledger.from_json(r) for r in self._get("/api/v1/ledgers", search=search)]

    def create_ledger(
        self,
        name: str,
        *,
        group_name: str | None = None,
        group_id: int | None = None,
        code: str | None = None,
    ) -> Ledger:
        payload: dict[str, Any] = {"name": name, "code": code}
        if group_id is not None:
            payload["group_id"] = group_id
        elif group_name is not None:
            payload["group_name"] = group_name
        return Ledger.from_json(self._post("/api/v1/ledgers", payload))

    def groups(self) -> list[Group]:
        return [Group.from_json(r) for r in self._get("/api/v1/groups")]

    # ------------------------------------------------------------- vouchers
    def vouchers(
        self,
        *,
        type_code: str = "",
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 200,
    ) -> list[Voucher]:
        rows = self._get(
            "/api/v1/vouchers",
            type=type_code,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None,
            limit=limit,
        )
        return [Voucher.from_json(r) for r in rows]

    def voucher(self, voucher_id: int) -> Voucher:
        return Voucher.from_json(self._get(f"/api/v1/vouchers/{voucher_id}"))

    def voucher_pdf(self, voucher_id: int) -> tuple[bytes, str]:
        """The printed voucher (ADR-013) as bytes + the server's filename."""
        response = self._client.get(f"/api/v1/vouchers/{voucher_id}/pdf")
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(_safe_json(response), response.text[:300]))
        name = "voucher.pdf"
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            name = disposition.split("filename=", 1)[1].strip('" ')
        return response.content, name

    def create_voucher(
        self,
        *,
        voucher_type: str,
        voucher_date: date,
        entries: list[dict],
        narration: str | None = None,
        client_token: str | None = None,
    ) -> Voucher:
        payload = {
            "voucher_type": voucher_type,
            "voucher_date": voucher_date.isoformat(),
            "narration": narration or None,
            "entries": entries,
        }
        if client_token:
            payload["client_token"] = client_token
        return Voucher.from_json(self._post("/api/v1/vouchers", payload))

    # -------------------------------------------------------------- reports
    def trial_balance(self, as_of: date | None = None) -> dict:
        data = self._get("/api/v1/reports/trial-balance", as_of=as_of)
        data["ledgers"] = [TrialBalanceRow.from_json(r) for r in data.get("ledgers", [])]
        return data

    def profit_loss(self, date_from: date, date_to: date) -> dict:
        return self._get(
            "/api/v1/reports/profit-loss",
            **{"from": date_from.isoformat(), "to": date_to.isoformat()},
        )

    def balance_sheet(self, as_of: date | None = None) -> dict:
        return self._get("/api/v1/reports/balance-sheet", as_of=as_of)

    def fy_close(self, as_of: date) -> dict:
        return self._get("/api/v1/reports/fy-close", as_of=as_of.isoformat())

    def pack(
        self, key: str, *, format_: str | None = None, **params: str
    ) -> tuple[bytes, str, str]:
        """A pack report (ADR-014) — PDF bytes by default, ``format_=csv`` for
        the machine format. Returns (content, media_type, filename)."""
        query = dict(params)
        if format_:
            query["format"] = format_
        response = self._client.get(f"/api/v1/reports/pack/{key}", params=query)
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(_safe_json(response), response.text[:300]))
        name = f"{key}"
        disposition = response.headers.get("content-disposition", "")
        if "filename=" in disposition:
            name = disposition.split("filename=", 1)[1].strip('" ')
        return (
            response.content,
            response.headers.get("content-type", "application/octet-stream"),
            name,
        )

    # -------------------------------------------------------------- periods
    def periods(self) -> list[Period]:
        return [Period.from_json(r) for r in self._get("/api/v1/periods")]

    def lock_period(self, period_id: int) -> Period:
        return Period.from_json(self._post(f"/api/v1/periods/{period_id}/lock"))

    def unlock_period(self, period_id: int) -> Period:
        return Period.from_json(self._post(f"/api/v1/periods/{period_id}/unlock"))

    def close_period(self, period_id: int) -> Period:
        return Period.from_json(self._post(f"/api/v1/periods/{period_id}/close"))

    def close_preview(self, period_id: int) -> dict:
        return self._get(f"/api/v1/periods/{period_id}/close-preview")

    # ---------------------------------------------------------------- slots
    def slots(self) -> list[Slot]:
        return [Slot.from_json(r) for r in self._get("/api/v1/slots")["slots"]]

    # ------------------------------------------------------------------ ops
    def scale_out(self) -> dict:
        return self._get("/api/v1/ops/scale-out")


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
