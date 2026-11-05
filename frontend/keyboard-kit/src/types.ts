/**
 * LedgerForge keyboard-kit — shared types.
 *
 * The interaction model (docs/keyboard-ux.md, ADR-004): UI registers
 * *commands*; keymaps are data binding combos to commands; a single
 * capture-phase pipeline resolves keys against a stack of focus scopes.
 */

export type ContextPredicate = (ctx: KeymapContext) => boolean;

/** Snapshot of the interaction state, evaluated on every keydown. */
export interface KeymapContext {
  /** Deepest active focus scope, e.g. 'voucherEditor' (null = none). */
  scopeId: string | null;
  /** Full scope stack, deepest first. */
  scopeIds: string[];
  /** True when an editable field (input/textarea/select) has focus. */
  inputFocused: boolean;
  /** Extension point for host applications. */
  [key: string]: unknown;
}

/** A user-invokable action. UI and plugins register these — never raw keys. */
export interface Command {
  /** Dotted id, e.g. 'voucher.accept'. */
  id: string;
  /** Human title — powers the command palette and screen-reader labels. */
  title: string;
  run(args?: unknown): void | Promise<unknown>;
  /** Availability predicate; unavailable commands are not matched. */
  when?: ContextPredicate;
}

/** A normalized key combination. */
export interface KeyCombo {
  /** Normalized key: 'a', 'Enter', 'F5', 'ArrowDown', ... */
  key: string;
  ctrl: boolean;
  meta: boolean;
  alt: boolean;
  shift: boolean;
}

/** A binding of a combo (or chord sequence) to a command. */
export interface Keybinding {
  /** Chord sequence — one combo, or two for a chord like ['K', 'K']. */
  combo: KeyCombo[];
  command: string;
  when?: ContextPredicate;
  /** Higher wins; defaults to the depth of the scope that registered it. */
  weight?: number;
}
