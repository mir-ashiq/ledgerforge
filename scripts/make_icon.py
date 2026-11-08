"""Generate the LedgerForge application icon, every platform's format.

Run:  python scripts/make_icon.py            # the .ico (Windows)
      python scripts/make_icon.py --icns     # + the .icns (macOS)
      python scripts/make_icon.py --png      # + the .png (Linux desktop)

Draws the glyph with Pillow — a stylized ledger book with an LF spine — and
writes a multi-resolution .ico (16–256 px) that the PyInstaller bundle and the
Qt window (QLockFile's neighbours, taskbar, title bar) share. Deterministic:
same input, same bytes, so releases diff cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "desktop" / "ledgerforge.ico"

# The brand palette (docs/style.md): forge-blue ground, brass-amber leaf.
GROUND = (23, 42, 65, 255)  # #152A41
PAGE = (246, 243, 235, 255)  # #F6F3EB
SPINE = (191, 144, 33, 255)  # #BF9021
INK = (21, 30, 44, 255)  # #151E2C


def draw_glyph(size: int) -> Image.Image:
    """The icon at any size: a closed ledger with a brass spine and LF rules."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0  # design grid: 256

    # rounded-square ground
    r = 46 * s
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=r, fill=GROUND)

    # the ledger's page, slightly rotated feel via asymmetric spine band
    d.rounded_rectangle([62 * s, 48 * s, 212 * s, 208 * s], radius=10 * s, fill=PAGE)

    # brass spine down the left edge of the page
    d.rounded_rectangle([62 * s, 48 * s, 88 * s, 208 * s], radius=10 * s, fill=SPINE)

    # ruled lines — double-entry rows
    for i, y in enumerate((92, 124, 156, 188)):
        d.line([104 * s, y * s, 196 * s, y * s], fill=INK, width=max(1, round(8 * s)))
        if i % 2 == 0:  # the debit column ticks
            d.rectangle([150 * s, (y - 12) * s, 162 * s, (y + 4) * s], fill=SPINE)

    return img


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    draw_glyph(256).save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=[draw_glyph(s) for s in sizes[:-1]],
    )
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {sizes})")

    if "--icns" in sys.argv:  # the macOS bundle's icon
        icns = OUT.with_suffix(".icns")
        draw_glyph(512).save(icns, format="ICNS")
        print(f"wrote {icns} ({icns.stat().st_size} bytes)")

    if "--png" in sys.argv:  # the Linux desktop file's icon
        png = OUT.with_suffix(".png")
        draw_glyph(256).save(png, format="PNG")
        print(f"wrote {png} ({png.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
