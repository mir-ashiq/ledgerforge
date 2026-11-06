"""LedgerForge — a community-driven, keyboard-first, double-entry accounting engine.

Inspired by TallyPrime. The three financial laws (docs/analysis.md §7):
1. Total debits must equal total credits for every transaction, exactly.
2. Financial history is append-only; corrections happen via reversal vouchers.
3. All money is arbitrary-precision decimal — floating point is banned.
"""

__version__ = "0.4.0"

#: Version of the stable plugin API surface (semver; see docs/plugin-guide.md).
#: 0.2 — DraftLine gained the optional FX snapshot fields
#:       (currency/amount_fx/fx_rate, all-or-none); transformers that rebuild
#:       lines must keep the trio consistent or drop it (issue #4).
PLUGIN_API_VERSION = "0.2"
