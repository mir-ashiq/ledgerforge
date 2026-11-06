# @ledgerforge/web

The voucher-entry screen — LedgerForge's first real screen, built on
[`@ledgerforge/keyboard-kit`](../keyboard-kit) per [docs/keyboard-ux.md](../../docs/keyboard-ux.md) and **ADR-004**. A full sale posts mouse-free at typing speed.

## Run it

```bash
# engine on :8000 (from the repo root)
make dev

# UI on :5173 (proxies /api to the engine)
cd frontend/web
npm install
npm run dev
```

Or the whole stack containerized: `docker compose up` → http://localhost:5173.

## The keyboard contract

| Keys | Action |
|---|---|
| typing in Particulars | filters the ledger picker (exact → prefix → substring) |
| `Enter` | accept field → next cell → next line (auto-appends rows) |
| `Shift+Enter` | previous cell |
| `↑` `↓` | move the picker highlight |
| `Ctrl+Enter` | fill the exact balancing amount into the current cell |
| `Ctrl+A` | accept the voucher (idempotent POST, amounts as strings) |
| `Alt+C` | create the missing ledger here, focus returns to the grid |
| `Alt+D` | delete the current line |
| `Alt+N` | new voucher (asks before discarding a dirty draft) |
| `F2` | change the voucher date |
| `F4`–`F9` | Contra · Payment · Receipt · Journal · Sales · Purchase |
| `Ctrl+K` | keyboard reference (live from the keymap) |
| `Esc` | back — close picker → close dialog → (gateway later) |

## How it holds the financial laws

- **Law 1 (Debits = Credits, exactly):** the auto-balance HUD and the
  pre-post validation run on exact decimal string math (`src/money/exact.ts`,
  BigInt-based — no floats, ever). Accept is blocked with the exact
  difference; the engine re-verifies in the ACID transaction.
- **Law 3 (exact decimals):** amount cells are strings end-to-end; the JSON
  payload carries `"1180.00"`-style strings, mirroring the server's
  string-only `Amount` type.

## Code map

```
src/
  money/exact.ts        exact decimal parse/add/sub/cmp/format (BigInt)
  voucher/model.ts      draft types, voucher types, date helpers
  voucher/reducer.ts    pure grid state machine (Enter-advance, auto-append…)
  voucher/validate.ts   exact totals + Law-1 pre-post validation
  voucher/pickerFilter.ts   type-ahead ranking
  api/                  fetch client + DTOs (amounts as strings)
  components/           VoucherScreen, VoucherGrid (virtualized rows),
                        LedgerPicker, create/date dialogs, BalanceHud, …
tests/
  money.spec.ts, reducer.spec.ts, pickerFilter.spec.ts   unit tests
  voucherScreen.spec.tsx  acceptance: a full sale posted mouse-free
                          (real key events through the keyboard-kit pipeline)
```

`npm test` · `npm run typecheck` · `npm run build`
