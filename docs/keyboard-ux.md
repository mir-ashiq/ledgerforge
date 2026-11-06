# Keyboard-First UX Plan — "The Gateway Model"

**Status:** Phase-1 plan (accepted) · **Reference implementation:** `frontend/keyboard-kit` · **Analysis:** `docs/analysis.md` §6 · **ADR-004**

This document is the strategy for a mouse-free web interface that replicates TallyPrime's voucher-entry velocity. It defines the interaction contract, the event-listener framework, the default keymap, the voucher grid, and the latency budget. The framework-agnostic TypeScript core of everything described here exists in `frontend/keyboard-kit/`.

---

## 1. The interaction contract

TallyPrime's speed is a *contract*, not a collection of shortcuts. LedgerForge adopts it verbatim:

| Principle | Rule |
|---|---|
| **Two navigation verbs** | `Enter` = forward (accept field → next field → submit). `Esc` = back (leave field → leave screen → previous menu). There is no third verb. |
| **One keystroke per menu item** | Gateway menus navigate by highlighted letter; no arrow hunting. |
| **Mode switches are F-keys** | `F2` date, `F4` Contra, `F5` Payment, `F6` Receipt, `F7` Journal, `F8` Sales, `F9` Purchase. |
| **Masters created mid-flight** | `Alt+C` inside a ledger field creates the missing ledger and returns focus to the grid — the flow never leaves the voucher. |
| **Zero-latency selection** | Ledger pickers run type-ahead over an in-memory index of the whole chart of accounts. |
| **Accept / destroy as chords** | `Ctrl+A` accepts the voucher; `Alt+D` deletes the current line. |

## 2. Architecture — the event-listener framework

Four collaborating pieces, all framework-agnostic TypeScript; React bindings are a thin layer (§5). One capture-phase listener owns every key event — widgets never attach their own handlers, eliminating double-handled-key races.

```
        document (capture: true)
              │  keydown
              ▼
   ┌──────────────────────────┐
   │  KeymapService            │  1. IME guard: swallow during composition
   │  (single pipeline)        │  2. Build context from FocusStack
   │                           │  3. KeybindingResolver.match(key, context)
   │                           │     (chords first, deepest scope wins)
   └──────────┬───────────────┘
              │ command id + args
              ▼
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │  CommandRegistry          │        │  FocusScopeStack          │
   │  id → {title, run, when}  │        │  push/pop scopes; roving  │
   └──────────────────────────┘        │  focus; Esc pops the top  │
                                        └──────────────────────────┘
```

### 2.1 Command Registry

```ts
export interface Command {
  id: string;                 // 'voucher.accept', 'ledger.create.here'
  title: string;              // human title — powers the command palette & a11y
  run: (args?: unknown) => void | Promise<unknown>;
  when?: ContextPredicate;    // availability predicate, e.g. (ctx) => ctx.inVoucherEditor
}
```

UI and plugins **register commands, never keys**. Keys are data (the keymap); commands are behavior. This indirection gives palette discovery, user rebinding, macro recording (roadmap) and plugin actions for free.

### 2.2 Keybinding resolver

```ts
export interface Keybinding {
  key: KeyCombo;              // parsed from 'Ctrl+A', 'Alt+C', 'K K' (chord)
  command: string;
  when?: ContextPredicate;    // e.g. (ctx) => ctx.scope === 'voucherEditor'
  weight?: number;            // manual override; default = scope depth
}
```

- **Chords:** `'K K'` = press `K`, then `K` within 2 s. Chords always beat single combos.
- **Platform normalization:** `Primary` maps to `Ctrl` (Windows/Linux) or `⌘` (macOS).
- **Resolution:** deepest focus scope wins; a binding with `when` beats a bare one at equal depth; nothing beats plain typing except explicit combos — text entry is never stolen.
- **Reserved chords avoided:** `Ctrl+W/N/T` are browser-owned and never bound.

### 2.3 Focus scope stack

```ts
interface FocusScope {
  id: string;                          // 'gateway' | 'voucherEditor' | 'ledgerPicker'
  element: HTMLElement;                // the container that owns focus
  onEscape(): boolean;                 // false = let the next scope handle it
  rovingItems?: () => HTMLElement[];   // for ArrowUp/Down + aria-activedescendant
}
```

Screens `push`/`pop` scopes; `Esc` pops the top; arrow keys move roving focus within a scope. Focus is explicit, visible, and announced — this is simultaneously the accessibility layer.

### 2.4 The pipeline (KeymapService)

1. If `event.isComposing` or between `compositionstart`/`compositionend` → return (never eat IME text).
2. Ignore pure modifier presses.
3. Match pending chord first, then single combos against the context (scope stack + flags like `inputFocused`).
4. On match: `event.preventDefault()`, `execute(command)`.
5. On no match: default browser behavior (typing).

## 3. Default keymap (`tallyKeymap.ts`)

| Combo | Command | When |
|---|---|---|
| `Enter` | `focus.forward` | always |
| `Esc` | `focus.back` | always |
| `Ctrl+A` | `voucher.accept` | voucherEditor |
| `Alt+C` | `ledger.create.here` | ledger field focused |
| `Alt+D` | `voucher.deleteLine` | voucherEditor |
| `F2` | `voucher.changeDate` | voucherEditor |
| `F4` | `voucher.setType.contra` | voucherEditor |
| `F5` | `voucher.setType.payment` | voucherEditor |
| `F6` | `voucher.setType.receipt` | voucherEditor |
| `F7` | `voucher.setType.journal` | voucherEditor |
| `F8` | `voucher.setType.sales` | voucherEditor |
| `F9` | `voucher.setType.purchase` | voucherEditor |
| `Ctrl+Enter` | `selection.alter` | picker open |
| `Ctrl+P` | `report.print` | report view |
| `Ctrl+K` | `palette.toggle` | always |
| `Alt+N` | `voucher.new` | gateway |

Users rebind with `keymap.set('voucher.accept', 'Ctrl+S')`; the setting persists and the palette/help always reflects the live map.

## 4. The voucher entry grid

- **Virtualized rows**; `Enter` advances cell-by-cell and auto-appends a row after the last cell — line entry never breaks.
- **Ledger picker:** typing filters the in-memory ledger index (exact → prefix → substring ranking); `↑/↓` + `Enter` selects; `Alt+C` creates a new ledger in place via `POST /api/v1/ledgers` without leaving the grid.
- **Amount cells are strings.** Display formatting via `Intl.NumberFormat` with fixed scale; arithmetic never happens client-side; JSON amounts are strings (Law 3).
- **Auto-balance HUD:** footer shows `Amount left: 250.00`, computed with exact decimal string math; the balancing line is one `Enter` away.
- **Accept:** `Ctrl+A` posts with an idempotency key; optimistic "Posting…" state; on success the HUD clears and a live region announces "Voucher SAL-2026-27-00042 posted" for screen readers.

## 5. React bindings (host integration)

```tsx
// A Tally-style voucher screen in ~20 lines of UI code
<KeyScope id="voucherEditor" onEscape={popScreen}>
  <useCommand id="voucher.accept" title="Accept voucher" run={post} when={inEditor} />
  <VoucherGrid lines={lines} />
  <LedgerPicker index={ledgerIndex} />
</KeyScope>
```

`useCommand` registers on mount, disposes cleanly; `<KeyScope>` pushes/pops the focus scope. The core (`commandRegistry`, `keybindings`, `keymapService`, `focusStack`) has zero React imports — a Svelte or web-components host can adopt it unchanged.

## 6. Latency budget & data strategy

| Interaction | Budget | Technique |
|---|---|---|
| Keystroke → echo | < 16 ms | local echo, controlled inputs, zero per-key network |
| Ledger picker open | < 50 ms | in-memory index (a book has thousands of ledgers, not millions) |
| Voucher accept ack | < 300 ms P50 | single POST, optimistic state, idempotent retry |
| Report open | < 1 s | server-side SQL functions (`rpt.*`), streamed render |
| Cold load | < 2 s | Vite code splitting, HTTP cache, masters prefetched |

Ledger index freshness: prefetch at login; invalidate via websocket events (`master.created`, `voucher.posted`) — the same domain events the plugin system emits (ADR-003).

## 7. Accessibility

Keyboard-first *is* the accessibility architecture: commands carry human titles (the palette is a screen-reader command list), roving tabindex preserves grid semantics (`role="grid"`), focus is always on exactly one element, and `aria-live` regions announce posting outcomes. Contrast, reduced-motion and font-scale settings follow standard host practice.

## 8. Delivery phases

1. **Done (v0.1):** framework-agnostic core + React bindings + default keymap + this plan.
2. **Done (v0.2):** voucher grid + ledger picker components against the live API — `frontend/web` (virtualized rows, type-ahead picker, `Alt+C` create-ledger-here, auto-balance HUD with exact decimal string math, idempotent accept; acceptance-tested end-to-end, mouse-free).
3. **Done (v0.3):** command palette UI (`Ctrl+K`: filter, `Enter` run, `Ctrl+R` rebind, `Alt+R` reset), keybinding persistence (`KeymapStore`, localStorage in `frontend/web`; user bindings reserve their combo, displaced commands fall back and are marked "conflicts"), rebind capture mode in the core pipeline (defining a combo can never trigger the command it used to fire).
4. **Later:** macro recording, offline queuing of accepted vouchers (service worker + idempotency keys make this safe), plugin command registration into the palette.
