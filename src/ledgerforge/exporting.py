"""The export seam (ADR-030, roadmap Phase 9): every report and list
surface speaks CSV and XLSX through one renderer pair.

Column sets are declared beside each route (pydantic list routes derive
theirs from the model); amounts render as the engine's exact decimal
digits — CSV as plain text, XLSX as numeric cells serialized from the
Decimal's own string (the file carries exact digits; the engine stays the
source of truth, Law 3). XLSX arrives through openpyxl (pure Python, a
core dependency since v0.27.0); legacy ``.xls`` is deliberately not read
or written — xlsx is the only Excel dialect TallyPrime and every modern
spreadsheet uses.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

#: The formats the seam serves; ``?format=`` on any exportable GET.
EXPORT_FORMATS = ("csv", "xlsx")

_MEDIA_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def export_format(request) -> str | None:
    """The requested export format, or None when the caller wants JSON.

    Accepts ``?format=csv`` / ``?format=xlsx`` (case-insensitive);
    anything else is a 422 naming the two — an exportable surface never
    silently ignores a format it was asked for.
    """
    raw = request.query_params.get("format")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized not in EXPORT_FORMATS:
        from .errors import ValidationError

        raise ValidationError(
            f"unknown export format {raw!r} — use one of: {', '.join(EXPORT_FORMATS)}"
        )
    return normalized


def safe_stem(name: str) -> str:
    """A download-safe filename stem (printing.py's rule)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:80] or "export"


def _cell(value: Any) -> Any:
    """One cell's value: Decimals pass through (openpyxl serializes the
    exact digits), None empties, bools render Yes/No the way every list
    surface already shows them, everything else verbatim."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return value


def _renderable(value: Any) -> Any:
    """The CSV variant: Decimals render with ``str()`` — exact digits,
    never a float round-trip."""
    if isinstance(value, Decimal):
        return str(value)
    return _cell(value)


def rows_to_csv(columns: Sequence[str], rows: Sequence[dict]) -> str:
    """Render rows (dicts keyed by column) as CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_renderable(row.get(column)) for column in columns])
    return buffer.getvalue()


def rows_to_xlsx(columns: Sequence[str], rows: Sequence[dict], sheet: str = "Sheet") -> bytes:
    """Render rows as an .xlsx workbook: one sheet, frozen bold header.
    Decimal cells serialize from the Decimal's own string, so the file
    carries the engine's exact digits."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = safe_stem(sheet)[:31] or "Sheet"
    worksheet.append([str(column) for column in columns])
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        worksheet.append([_cell(row.get(column)) for column in columns])
    worksheet.freeze_panes = "A2"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def export_response(
    fmt: str,
    *,
    columns: Sequence[str],
    rows: Sequence[dict],
    stem: str,
    sheet: str | None = None,
):
    """A FastAPI Response carrying the rendered export — the banking
    precedent's Content-Disposition shape."""
    from fastapi import Response

    stem = safe_stem(stem)
    if fmt == "xlsx":
        return Response(
            content=rows_to_xlsx(columns, rows, sheet=sheet or stem),
            media_type=_MEDIA_TYPES["xlsx"],
            headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'},
        )
    return Response(
        content=rows_to_csv(columns, rows),
        media_type=_MEDIA_TYPES["csv"],
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
    )


def model_columns(model_cls) -> list[str]:
    """Column names from a pydantic Out model — the list surfaces' exports
    derive theirs from the model, so a field added to the Out model
    exports without a second declaration."""
    return list(model_cls.model_fields)


def list_export(request, items, model_cls, stem: str):
    """``?format=`` for a pydantic list surface: None when the caller
    wants JSON, the rendered Response otherwise (ADR-030 §3)."""
    fmt = export_format(request)
    if fmt is None:
        return None
    columns = model_columns(model_cls)
    return export_response(fmt, columns=columns, rows=model_rows(items, columns), stem=stem)


def rows_export(request, rows, columns, stem: str):
    """``?format=`` for a dict-list surface with explicit columns."""
    fmt = export_format(request)
    if fmt is None:
        return None
    return export_response(fmt, columns=columns, rows=model_rows(rows, columns), stem=stem)


def model_rows(items: Sequence[Any], columns: Sequence[str]) -> list[dict]:
    """Rows from pydantic Out models (or plain dicts) by column name."""
    rows: list[dict] = []
    for item in items:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        rows.append({column: data.get(column) for column in columns})
    return rows


# ----------------------------------------------------- the report projections
# Every report surface's payload → (columns, rows). Pure functions: the
# tests pin them, and the API layer stays a three-line addition.


def _rows_from(payload: dict, key: str, columns: Sequence[str]) -> tuple[list[str], list[dict]]:
    return list(columns), model_rows(payload.get(key) or [], columns)


def trial_balance_projection(payload: dict) -> tuple[list[str], list[dict]]:
    return _rows_from(
        payload,
        "ledgers",
        ("ledger_name", "group_name", "nature", "dr", "cr", "balance", "dc"),
    )


def profit_loss_projection(payload: dict) -> tuple[list[str], list[dict]]:
    rows: list[dict] = []
    trading = payload.get("trading") or {}
    pl = payload.get("profit_and_loss") or {}
    for section, bucket, side in (
        ("direct_incomes", "Direct Incomes", "Cr"),
        ("direct_expenses", "Direct Expenses", "Dr"),
    ):
        for group in trading.get(section) or []:
            rows.append(
                {
                    "section": bucket,
                    "group": group["group_name"],
                    "amount": group["amount"],
                    "side": side,
                }
            )
    rows.append(
        {
            "section": "Trading",
            "group": "Gross Profit",
            "amount": trading.get("gross_profit"),
            "side": "Cr",
        }
    )
    for section, bucket, side in (
        ("indirect_incomes", "Indirect Incomes", "Cr"),
        ("indirect_expenses", "Indirect Expenses", "Dr"),
    ):
        for group in pl.get(section) or []:
            rows.append(
                {
                    "section": bucket,
                    "group": group["group_name"],
                    "amount": group["amount"],
                    "side": side,
                }
            )
    rows.append(
        {
            "section": "Profit & Loss",
            "group": "Net Profit",
            "amount": pl.get("net_profit"),
            "side": "Cr",
        }
    )
    return ["section", "group", "amount", "side"], rows


def balance_sheet_projection(payload: dict) -> tuple[list[str], list[dict]]:
    rows: list[dict] = []
    for section in ("assets", "liabilities", "equity"):
        block = payload.get(section) or {}
        for group in block.get("groups") or []:
            rows.append(
                {
                    "section": section.title(),
                    "group": group["group_name"],
                    "amount": group["amount"],
                }
            )
        if section == "equity":
            rows.append(
                {
                    "section": "Equity",
                    "group": "Net Profit (to date)",
                    "amount": block.get("net_profit_to_date"),
                }
            )
        rows.append(
            {
                "section": section.title(),
                "group": f"Total {section.title()}",
                "amount": block.get("total"),
            }
        )
    return ["section", "group", "amount"], rows


def fy_close_projection(payload: dict) -> tuple[list[str], list[dict]]:
    return _rows_from(payload, "lines", ("bucket", "ledger_name", "dc", "amount"))


def cost_summary_projection(payload: dict) -> tuple[list[str], list[dict]]:
    columns = ("centre_name", "category_name", "dr", "cr", "net")
    rows = model_rows(payload.get("centres") or [], columns)
    unallocated = payload.get("unallocated") or {}
    if unallocated:
        rows.append(
            {
                "centre_name": "Unallocated",
                "category_name": "",
                "dr": unallocated.get("dr"),
                "cr": unallocated.get("cr"),
                "net": unallocated.get("net"),
            }
        )
    return list(columns), rows


def cost_breakup_projection(payload: dict) -> tuple[list[str], list[dict]]:
    return _rows_from(payload, "ledgers", ("ledger_name", "dr", "cr", "net"))


def bank_lines_projection(payload: dict) -> tuple[list[str], list[dict]]:
    columns = ("value_date", "dc", "amount", "reference", "narrative", "cheque_no")
    rows: list[dict] = []
    for line in payload.get("lines") or []:
        match = line.get("match")
        rows.append(
            {
                "value_date": line.get("value_date"),
                "dc": line.get("dc"),
                "amount": line.get("amount"),
                "reference": line.get("reference"),
                "narrative": line.get("narrative"),
                "cheque_no": line.get("cheque_no"),
                "matched": "Yes" if (match and match.get("live")) else "No",
                "voucher_no": (match or {}).get("voucher_no") if match else None,
            }
        )
    return list(columns) + ["matched", "voucher_no"], rows


def gst_lines_projection(payload: dict) -> tuple[list[str], list[dict]]:
    line_rows = payload.get("lines") or []
    if not line_rows:
        return [], []
    columns = list(line_rows[0].keys())
    return columns, model_rows(line_rows, columns)


def attendance_projection(payload: dict) -> tuple[list[str], list[dict]]:
    return _rows_from(
        payload,
        "entries",
        ("employee", "present", "absent", "half", "leave_paid", "note"),
    )


def csv_to_rows(csv_text: str) -> tuple[list[str], list[dict]]:
    """A pack report's own CSV, re-read into columns and rows — the
    xlsx path for pack reports keeps the pack's columns verbatim."""
    import csv as _csv
    import io as _io

    reader = _csv.reader(_io.StringIO(csv_text))
    header = next(reader, None)
    if header is None:
        return [], []
    columns = [str(cell) for cell in header]
    rows = [dict(zip(columns, row, strict=False)) for row in reader]
    return columns, rows
