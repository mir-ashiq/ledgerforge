# @ledgerforge/keyboard-kit

The framework-agnostic interaction core for LedgerForge's mouse-free UI (**ADR-004**, full plan in [docs/keyboard-ux.md](../../docs/keyboard-ux.md)).

Four pieces, one contract:

| Piece | File | Role |
|---|---|---|
| **Command Registry** | `commandRegistry.ts` | every action is `Command { id, title, run, when }` — UI and plugins register commands, never raw keys |
| **Keybinding Resolver** | `keybindings.ts` | parse/match combos ('Ctrl+A', chords like 'K K'), platform normalization (`Primary` = Cmd/Ctrl), IME-safe |
| **KeymapService** | `keymapService.ts` | the single capture-phase `keydown` pipeline: IME guard → context → resolve → `preventDefault` + execute |
| **Focus Scope Stack** | `focusStack.ts` | `Enter` forward, `Escape` pops the deepest scope — TallyPrime's two-verb navigation |

Plus **React bindings** (`react/bindings.ts`: `useCommand`, `useFocusScope`, `useRovingFocus`) and the **Tally preset** (`presets/tallyKeymap.ts`: `Ctrl+A` accept, `Alt+C` create-ledger-here, `Alt+D` delete line, `F2` date, `F4–F9` voucher types…).

## Usage

```ts
import { KeymapService } from '@ledgerforge/keyboard-kit';
import { tallyKeymap } from '@ledgerforge/keyboard-kit/presets/tallyKeymap';

const keymap = new KeymapService();

keymap.registerCommand({
  id: 'voucher.accept',
  title: 'Accept voucher',
  run: () => postVoucher(),
});
keymap.bind('Primary+A', 'voucher.accept');

keymap.start(); // attaches the single capture-phase listener
```

React hosts use the adapters:

```tsx
import { keymap, useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';

useFocusScope('voucherEditor'); // Escape pops back, scope ids feed `when` predicates
useCommand({ id: 'voucher.accept', title: 'Accept voucher', run: post }, 'Primary+A');
```

Hosts with dynamic UI state feed it into `when` predicates without re-binding:

```ts
const flagsRef = useRef<Record<string, unknown>>({});
keymap.setContextProvider(() => flagsRef.current);
// e.g. bind with: (ctx) => ctx.scopeId === 'voucherEditor' && ctx.pickerOpen
```

## Design rules

1. **One pipeline.** No widget attaches its own key handlers — this is what eliminates double-handled-key races.
2. **Plain typing is sacred.** Single-letter bindings only fire outside text fields unless they carry a modifier.
3. **IME safe.** Events during composition are ignored, so Hindi/CJK input never loses characters.
4. **Commands are the currency.** The command palette, user rebinding, plugin actions and a11y labels all fall out of the same registry.
