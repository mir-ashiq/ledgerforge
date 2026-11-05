/**
 * The default LedgerForge keymap — TallyPrime muscle memory (docs/keyboard-ux.md §3).
 *
 * Combos reference the 'Primary' modifier (Cmd on macOS, Ctrl elsewhere)
 * where a cross-platform chord is intended.
 */
import type { Keybinding } from '../types.js';
import { parseBindingSpec } from '../keybindings.js';

type Preset = [spec: string, command: string];

const PRESETS: Preset[] = [
  // The two navigation verbs
  ['Enter', 'focus.forward'],
  ['Escape', 'focus.back'],
  // Voucher editor
  ['Primary+A', 'voucher.accept'],
  ['Alt+C', 'ledger.create.here'],
  ['Alt+D', 'voucher.deleteLine'],
  ['F2', 'voucher.changeDate'],
  ['F4', 'voucher.setType.contra'],
  ['F5', 'voucher.setType.payment'],
  ['F6', 'voucher.setType.receipt'],
  ['F7', 'voucher.setType.journal'],
  ['F8', 'voucher.setType.sales'],
  ['F9', 'voucher.setType.purchase'],
  ['Alt+N', 'voucher.new'],
  // Pickers / lists
  ['ArrowUp', 'focus.moveUp'],
  ['ArrowDown', 'focus.moveDown'],
  ['Ctrl+Enter', 'selection.alter'],
  // Global
  ['Primary+K', 'palette.toggle'],
  ['Primary+P', 'report.print'],
];

/** The default bindings, as data. Hosts may override via setKeybinding(). */
export function tallyKeymap(): Keybinding[] {
  return PRESETS.map(([spec, command]) => ({
    combo: parseBindingSpec(spec),
    command,
  }));
}

export const TALLY_PRESETS = PRESETS;
