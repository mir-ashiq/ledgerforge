"""``python -m ledgerforge.nativeapp`` — the same entry as the exe."""

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
