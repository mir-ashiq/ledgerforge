"""The print bridge: any voucher as a PDF (ADR-013).

Three layers, one wire format:

1. **A minimal PDF 1.4 writer** — zero dependencies. Business documents are
   text and rules, so the writer draws exactly that: strings, horizontal
   rules, page boxes. Fonts are the PDF base-14 (Helvetica, Helvetica-Bold)
   with ``WinAnsiEncoding`` — they live in every viewer, so there is nothing
   to embed and nothing to install. Glyphs outside cp1252 (``₹`` for one)
   degrade to ``?``; the layouts never need them (amounts are digits, the
   currency is the book's code in the header).
2. **A neutral layout engine** — ``DocumentView`` (plugin API 0.5) says
   *what* a document contains; this module decides *where it goes*:
   pagination, column geometry, alignment, page furniture. One visual
   language for payslips, tax invoices and voucher slips alike.
3. **Dispatch** — a voucher's ``meta`` keys are offered, in order, to the
   plugin registry's document-view factories; the first non-``None`` view
   wins. Anything unclaimed — plain vouchers, reversals, payouts — falls
   back to the voucher slip, so **every** voucher is printable.

Amounts reach this module as exact ``Decimal`` and are formatted once at
the edge (``format_amount``) — the writer draws strings only, never
arithmetic (Law 3).
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Book, Ledger, Voucher, VoucherEntry, VoucherType
from ..plugins import (
    DocumentColumn,
    DocumentContext,
    DocumentSection,
    DocumentView,
    EntryLine,
    Resolver,
)

if TYPE_CHECKING:
    from ..plugins import PluginManager

# ------------------------------------------------------------ page geometry
# PDF user space: 72 pt = 1 inch, origin bottom-left. The writer takes
# top-left-origin coordinates (y grows downward) and flips internally —
# the layout engine thinks like a typesetter, not like a printer.

PAGE_W, PAGE_H = 595.28, 841.89  # A4 portrait
MARGIN_X, MARGIN_TOP = 48.0, 56.0
#: content may not start below this y (room for the page footer)
CONTENT_BOTTOM = PAGE_H - 46.0
FOOTER_Y = PAGE_H - 26.0
#: the right edge of right-aligned content
RIGHT_X = PAGE_W - MARGIN_X

# --------------------------------------------------------- Helvetica metrics
# Advance widths in 1/1000 em for ASCII 32–126, from the Adobe core-14 AFM
# data for Helvetica and Helvetica-Bold (public, stable numbers). Widths
# drive right-alignment and ellipsize truncation only — an errant unit
# would shift a glyph by less than a point, never a digit.

_W_HELV = {
    " ": 278,
    "!": 278,
    '"': 355,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 667,
    "'": 191,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    "0": 556,
    "1": 556,
    "2": 556,
    "3": 556,
    "4": 556,
    "5": 556,
    "6": 556,
    "7": 556,
    "8": 556,
    "9": 556,
    ":": 278,
    ";": 278,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 556,
    "@": 1015,
    "A": 667,
    "B": 667,
    "C": 722,
    "D": 722,
    "E": 667,
    "F": 611,
    "G": 778,
    "H": 722,
    "I": 278,
    "J": 500,
    "K": 667,
    "L": 556,
    "M": 833,
    "N": 722,
    "O": 778,
    "P": 667,
    "Q": 778,
    "R": 722,
    "S": 667,
    "T": 611,
    "U": 722,
    "V": 667,
    "W": 944,
    "X": 667,
    "Y": 667,
    "Z": 611,
    "[": 278,
    "\\": 278,
    "]": 278,
    "^": 469,
    "_": 556,
    "`": 333,
    "a": 556,
    "b": 556,
    "c": 500,
    "d": 556,
    "e": 556,
    "f": 278,
    "g": 556,
    "h": 556,
    "i": 222,
    "j": 222,
    "k": 500,
    "l": 222,
    "m": 833,
    "n": 556,
    "o": 556,
    "p": 556,
    "q": 556,
    "r": 333,
    "s": 500,
    "t": 278,
    "u": 556,
    "v": 500,
    "w": 722,
    "x": 500,
    "y": 500,
    "z": 500,
    "{": 334,
    "|": 260,
    "}": 334,
    "~": 584,
}

_W_HELV_BOLD = {
    " ": 278,
    "!": 333,
    '"': 474,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 722,
    "'": 238,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    "0": 556,
    "1": 556,
    "2": 556,
    "3": 556,
    "4": 556,
    "5": 556,
    "6": 556,
    "7": 556,
    "8": 556,
    "9": 556,
    ":": 333,
    ";": 333,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 611,
    "@": 975,
    "A": 722,
    "B": 722,
    "C": 722,
    "D": 722,
    "E": 667,
    "F": 611,
    "G": 778,
    "H": 722,
    "I": 278,
    "J": 556,
    "K": 722,
    "L": 611,
    "M": 833,
    "N": 722,
    "O": 778,
    "P": 667,
    "Q": 778,
    "R": 722,
    "S": 667,
    "T": 611,
    "U": 722,
    "V": 667,
    "W": 944,
    "X": 667,
    "Y": 667,
    "Z": 611,
    "[": 333,
    "\\": 278,
    "]": 333,
    "^": 584,
    "_": 556,
    "`": 333,
    "a": 556,
    "b": 611,
    "c": 556,
    "d": 611,
    "e": 556,
    "f": 333,
    "g": 611,
    "h": 611,
    "i": 278,
    "j": 278,
    "k": 556,
    "l": 278,
    "m": 889,
    "n": 611,
    "o": 611,
    "p": 611,
    "q": 611,
    "r": 389,
    "s": 556,
    "t": 333,
    "u": 611,
    "v": 556,
    "w": 778,
    "x": 556,
    "y": 556,
    "z": 500,
    "{": 389,
    "|": 280,
    "}": 389,
    "~": 584,
}

_ELLIPSIS = "…"


def _width_table(bold: bool) -> dict[str, int]:
    return _W_HELV_BOLD if bold else _W_HELV


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """The rendered width of ``text`` at ``size`` (Helvetica metrics)."""
    table = _width_table(bold)
    total = sum(table.get(ch, 556) for ch in text)
    return total * size / 1000.0


def _fit(text: str, size: float, max_width: float, *, bold: bool = False) -> str:
    """Truncate with an ellipsis so the string fits ``max_width``."""
    if text_width(text, size, bold=bold) <= max_width:
        return text
    out = text
    while out and text_width(out + _ELLIPSIS, size, bold=bold) > max_width:
        out = out[:-1]
    return out + _ELLIPSIS if out else _ELLIPSIS


def format_amount(value: Decimal) -> str:
    """Money for display: quantized to cents, thousands-separated. Never
    ``-0.00`` — a negative half-cent rounds to a clean zero."""
    quantized = Decimal(value).quantize(Decimal("0.01"))
    if not quantized:
        quantized = Decimal("0.00")
    return f"{quantized:,.2f}"


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _latin(text: str) -> str:
    """Base-14 + WinAnsiEncoding means cp1252; unmappable glyphs fall to '?'."""
    return text.encode("cp1252", errors="replace").decode("cp1252")


# --------------------------------------------------------------- the writer


class _Sheet:
    """One page under construction. All coordinates are top-left origin;
    ``y`` is the text baseline (or rule position), in points."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = 10.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        font = "F2" if bold else "F1"
        pdf_y = PAGE_H - y
        self.ops.append(
            f"{gray:g} g BT /{font} {size:g} Tf "
            f"1 0 0 1 {x:.2f} {pdf_y:.2f} Tm ({_pdf_escape(_latin(text))}) Tj ET"
        )

    def rule(
        self, x1: float, x2: float, y: float, *, width: float = 0.6, gray: float = 0.55
    ) -> None:
        pdf_y = PAGE_H - y
        self.ops.append(
            f"{gray:g} g {width:g} w {x1:.2f} {pdf_y:.2f} m {x2:.2f} {pdf_y:.2f} l 0 g S"
        )


class PdfWriter:
    """Assembles ``_Sheet`` pages into a valid PDF 1.4 byte stream.

    Object layout: 1 catalog, 2 page tree, 3 Helvetica, 4 Helvetica-Bold,
    then each page as a (page, content) pair. Content streams are
    uncompressed — a payslip's amounts are greppable in the bytes, which
    the tests (and the curious) appreciate.
    """

    def __init__(self, *, title: str = "") -> None:
        self.title = title
        self.pages: list[_Sheet] = []

    def add_page(self) -> _Sheet:
        sheet = _Sheet()
        self.pages.append(sheet)
        return sheet

    def build(self) -> bytes:
        if not self.pages:
            self.add_page()
        n = len(self.pages)
        # object numbers: 1 catalog · 2 pages · 3 F1 · 4 F2 · then pairs
        total = 4 + 2 * n

        chunks: list[bytes] = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
        offsets = [0] * (total + 1)
        position = len(chunks[0])

        def emit(number: int, body: bytes) -> None:
            nonlocal position
            offsets[number] = position
            piece = f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
            chunks.append(piece)
            position += len(piece)

        emit(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
        emit(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
        font_helv = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        font_bold = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold"
            b" /Encoding /WinAnsiEncoding >>"
        )
        emit(3, font_helv)
        emit(4, font_bold)
        for i, sheet in enumerate(self.pages):
            page_no, content_no = 5 + 2 * i, 6 + 2 * i
            page_obj = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_no} 0 R >>"
            )
            emit(page_no, page_obj.encode())
            stream = "\n".join(sheet.ops).encode("cp1252", errors="replace")
            body = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
            emit(content_no, body)

        xref_at = position
        lines = [f"xref\n0 {total + 1}\n0000000000 65535 f \n"]
        for number in range(1, total + 1):
            lines.append(f"{offsets[number]:010d} 00000 n \n")
        lines.append(f"trailer\n<< /Size {total + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n")
        chunks.append("".join(lines).encode())
        return b"".join(chunks)


# ------------------------------------------------------------ layout engine


class _Layout:
    """Draws one ``DocumentView`` across A4 pages: a fixed header block,
    flowing sections with repeated column headers across page breaks, a
    right-aligned totals block, footnotes and page furniture."""

    def __init__(self, view: DocumentView) -> None:
        self.view = view
        self.writer = PdfWriter(title=view.title)
        self.sheet = self.writer.add_page()
        self.y = MARGIN_TOP

    # -- page machinery -----------------------------------------------------
    def _break(self, needed: float) -> None:
        if self.y + needed > CONTENT_BOTTOM:
            self.sheet = self.writer.add_page()
            self.y = MARGIN_TOP + 6.0

    # -- blocks ---------------------------------------------------------------
    def header(self) -> None:
        v = self.view
        self.sheet.text(MARGIN_X, self.y + 13.0, v.title, size=15.0, bold=True)
        if v.reference:
            available = RIGHT_X - MARGIN_X - text_width(v.title, 15.0, bold=True) - 24.0
            ref = _fit(v.reference, 10.5, available, bold=True)
            x = RIGHT_X - text_width(ref, 10.5, bold=True)
            self.sheet.text(x, self.y + 13.0, ref, size=10.5, bold=True, gray=0.25)
        if v.subtitle:
            self.sheet.text(
                MARGIN_X,
                self.y + 27.0,
                _fit(v.subtitle, 9.5, RIGHT_X - MARGIN_X),
                size=9.5,
                gray=0.35,
            )
        self.y += 34.0
        self.sheet.rule(MARGIN_X, RIGHT_X, self.y, width=0.9, gray=0.1)
        self.y += 8.0

    def fields(self) -> None:
        columns = 2
        col_w = (RIGHT_X - MARGIN_X) / columns
        for index, (label, value) in enumerate(self.view.fields):
            if not value:
                continue
            if index % columns == 0:
                self._break(16.0)
            x = MARGIN_X + (index % columns) * col_w
            self.sheet.text(
                x, self.y + 10.5, _fit(f"{label}", 7.5, col_w - 8.0), size=7.5, gray=0.4
            )
            self.sheet.text(x, self.y + 21.5, _fit(str(value), 9.5, col_w - 8.0), size=9.5)
            if index % columns == 1:
                self.y += 25.0
        if self.view.fields:
            if len(self.view.fields) % columns:
                self.y += 25.0
            self.sheet.rule(MARGIN_X, RIGHT_X, self.y, width=0.4, gray=0.8)
            self.y += 14.0

    def section(self, section: DocumentSection) -> None:
        columns = section.columns
        widths = [c.width for c in columns]
        total_w = sum(widths) or 1.0
        content_w = RIGHT_X - MARGIN_X
        xs, ws = [], []
        cursor = MARGIN_X
        for w in widths:
            span = content_w * w / total_w
            xs.append(cursor)
            ws.append(span)
            cursor += span

        def header_row() -> None:
            self.sheet.text(
                MARGIN_X, self.y + 9.5, _fit(section.title, 10.0, content_w), size=10.0, bold=True
            )
            self.y += 17.0
            for col, x, w in zip(columns, xs, ws, strict=True):
                cell = _fit(col.header, 8.2, w - 6.0, bold=True)
                if col.align == "right":
                    self.sheet.text(x + w - 4.0, self.y + 9.0, cell, size=8.2, bold=True, gray=0.3)
                else:
                    self.sheet.text(x + 2.0, self.y + 9.0, cell, size=8.2, bold=True, gray=0.3)
            self.y += 5.0
            self.sheet.rule(MARGIN_X, RIGHT_X, self.y, width=0.5, gray=0.5)
            self.y += 12.0

        self._break(46.0)
        header_row()
        for row in section.rows:
            if self.y + 14.0 > CONTENT_BOTTOM:
                # a row never splits: fresh page, section header repeated
                self.sheet = self.writer.add_page()
                self.y = MARGIN_TOP + 6.0
                header_row()
            for col, x, w, cell in zip(columns, xs, ws, row, strict=False):
                text = _fit(str(cell), 9.3, w - 6.0)
                if col.align == "right":
                    self.sheet.text(x + w - 4.0, self.y + 9.0, text, size=9.3)
                else:
                    self.sheet.text(x + 2.0, self.y + 9.0, text, size=9.3)
            self.y += 13.5
        self.y += 9.0

    def totals(self) -> None:
        if not self.view.totals:
            return
        self._break(16.0 + 15.0 * len(self.view.totals))
        self.y += 2.0
        self.sheet.rule(RIGHT_X - 210.0, RIGHT_X, self.y, width=0.6, gray=0.6)
        self.y += 15.0
        for index, (label, value) in enumerate(self.view.totals):
            last = index == len(self.view.totals) - 1
            bold = last
            size = 10.5 if last else 9.5
            value_text = _fit(str(value), size, 118.0, bold=bold)
            self.sheet.text(RIGHT_X - 118.0, self.y, value_text, size=size, bold=bold)
            label_x = RIGHT_X - 122.0 - text_width(str(label), size, bold=bold)
            self.sheet.text(
                max(label_x, MARGIN_X),
                self.y,
                str(label),
                size=size,
                bold=bold,
                gray=0.1 if last else 0.35,
            )
            self.y += 15.5 if last else 14.5

    def footnotes(self) -> None:
        if not self.view.footnotes:
            return
        self._break(13.0 * len(self.view.footnotes) + 6.0)
        self.y += 6.0
        self.sheet.rule(MARGIN_X, RIGHT_X, self.y, width=0.4, gray=0.85)
        self.y += 13.0
        for note in self.view.footnotes:
            self._break(13.0)
            self.sheet.text(
                MARGIN_X,
                self.y + 9.0,
                _fit(f"• {note}", 7.3, RIGHT_X - MARGIN_X),
                size=7.3,
                gray=0.45,
            )
            self.y += 12.0

    def furniture(self) -> None:
        total = len(self.writer.pages)
        for number, sheet in enumerate(self.writer.pages, start=1):
            sheet.text(
                MARGIN_X,
                FOOTER_Y,
                "LedgerForge — exact, append-only accounting",
                size=7.2,
                gray=0.5,
            )
            sheet.text(
                RIGHT_X, FOOTER_Y, f"Page {number} of {total}", size=7.2, gray=0.5, bold=True
            )

    def render(self) -> bytes:
        self.header()
        self.fields()
        for section in self.view.sections:
            self.section(section)
        self.totals()
        self.footnotes()
        self.furniture()
        return self.writer.build()


def render_view(view: DocumentView) -> bytes:
    """A ``DocumentView`` as PDF bytes (one visual language for all packs)."""
    return _Layout(view).render()


# --------------------------------------------------------- the voucher slip


def voucher_slip(ctx: DocumentContext) -> DocumentView:
    """The universal fallback print: any voucher as a Dr/Cr slip. Reversals
    land here too (empty meta — ADR-010), with their mirror entries and a
    pointer to the voucher they undo."""
    debit = Decimal("0.00")
    credit = Decimal("0.00")
    rows: list[tuple[str, ...]] = []
    for entry in ctx.entries:
        debit_or_credit = format_amount(entry.amount) if entry.dc == "D" else ""
        credit_or_blank = format_amount(entry.amount) if entry.dc == "C" else ""
        if entry.dc == "D":
            debit += entry.amount
        else:
            credit += entry.amount
        rows.append(
            (
                str(entry.line_no),
                entry.ledger_name,
                entry.narration or "",
                debit_or_credit,
                credit_or_blank,
            )
        )

    fields: list[tuple[str, str]] = [
        ("Voucher type", ctx.voucher_type),
        ("Date", ctx.voucher_date.isoformat()),
        ("Posted at", ctx.posted_at.strftime("%Y-%m-%d %H:%M") if ctx.posted_at else ""),
    ]
    if ctx.narration:
        fields.append(("Narration", ctx.narration))
    if ctx.reversal_of is not None:
        fields.append(("Reversal of", f"voucher #{ctx.reversal_of}"))
    if ctx.meta:
        fields.append(("Documents", ", ".join(ctx.meta.keys())))

    return DocumentView(
        title="Voucher",
        subtitle=ctx.book_name,
        reference=ctx.voucher_no,
        fields=tuple(fields),
        sections=(
            DocumentSection(
                title="Entries",
                columns=(
                    DocumentColumn("#", width=0.05),
                    DocumentColumn("Ledger account", width=0.40),
                    DocumentColumn("Narration", width=0.27),
                    DocumentColumn("Debit", width=0.14, align="right"),
                    DocumentColumn("Credit", width=0.14, align="right"),
                ),
                rows=tuple(rows),
            ),
        ),
        totals=(
            ("Total debits", format_amount(debit)),
            ("Total credits", format_amount(credit)),
        ),
        footnotes=(
            "Equal debits and credits are enforced at commit (Law 1).",
            "This record is append-only — corrections are reversal vouchers (Law 2).",
        ),
    )


# ------------------------------------------------------------------ dispatch


def document_view_for(manager: PluginManager, ctx: DocumentContext) -> DocumentView:
    """Offer the voucher's meta keys to the registered document-view
    factories in order; the first view wins. Unclaimed (or declined)
    vouchers fall back to the voucher slip — everything is printable."""
    for key in ctx.meta:
        factory = manager.registry.document_views.get(key)
        if factory is None:
            continue
        view = factory(ctx)
        if view is not None:
            return view
    return voucher_slip(ctx)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "document"
    return cleaned[:80] + ".pdf"


def render_voucher_pdf(
    manager: PluginManager,
    session: Session,
    book: Book,
    voucher: Voucher,
    entries: list[VoucherEntry],
    params: dict[str, str],
) -> tuple[bytes, str]:
    """One posted voucher as ``(pdf_bytes, filename)`` — the whole of the
    ``GET /vouchers/{id}/pdf`` route. Ledger names resolve through the
    book-scoped session exactly as the JSON view does."""
    ledger_ids = {entry.ledger_id for entry in entries}
    names = {
        row.id: row.name for row in session.scalars(select(Ledger).where(Ledger.id.in_(ledger_ids)))
    }
    voucher_type = session.get(VoucherType, voucher.voucher_type_id)

    posted_at: datetime | None = voucher.posted_at
    ctx = DocumentContext(
        book_name=book.name,
        voucher_no=voucher.voucher_no,
        voucher_type=voucher_type.code if voucher_type else "",
        voucher_date=voucher.voucher_date,
        posted_at=posted_at,
        narration=voucher.narration,
        reversal_of=voucher.reversal_of,
        meta=dict(voucher.meta or {}),
        entries=tuple(
            EntryLine(
                line_no=entry.line_no,
                ledger_name=names.get(entry.ledger_id, f"ledger {entry.ledger_id}"),
                dc=entry.dc,  # type: ignore[arg-type]  # D/C enforced at insert
                amount=entry.amount,
                narration=entry.narration,
            )
            for entry in entries
        ),
        params={k: v for k, v in params.items() if v},
        resolver=Resolver(session, book),
    )
    view = document_view_for(manager, ctx)
    return render_view(view), _safe_filename(view.filename or voucher.voucher_no)


__all__ = [
    "DocumentContext",
    "DocumentSection",
    "DocumentView",
    "EntryLine",
    "PdfWriter",
    "document_view_for",
    "format_amount",
    "render_view",
    "render_voucher_pdf",
    "text_width",
    "voucher_slip",
]
