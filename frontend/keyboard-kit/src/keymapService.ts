/**
 * KeymapService — the single capture-phase keydown pipeline (ADR-004).
 *
 * One listener on the document sees every key before widget code:
 *   1. IME guard (never eat composition input)
 *   2. Build context from the focus scope stack
 *   3. Resolve: pending chord first, then bindings — chords beat singles,
 *      deeper scope weight wins, `when` predicates must pass
 *   4. On match: preventDefault + execute command; else let the browser type
 *
 * Bindings come in two layers (keyboard-ux §8 phase 3):
 *   - *defaults*, registered by the UI every time a screen mounts, and
 *   - *user bindings*, set via `setKeybinding` and persisted through a
 *     `KeymapStore`. One physical combo always triggers exactly one command:
 *     a user binding reserves its combo — colliding defaults (even ones that
 *     register later, on remount) are skipped, and a displaced command falls
 *     back to its own default. `resetKeybinding`/`resetAllKeybindings`
 *     restore the shipped map.
 */

import { CommandRegistry } from './commandRegistry.js';
import { FocusStack } from './focusStack.js';
import {
  comboFromEvent,
  comboMatches,
  isComposing,
  parseBindingSpec,
} from './keybindings.js';
import type { Command, ContextPredicate, Keybinding, KeyCombo, KeymapContext } from './types.js';

const CHORD_TIMEOUT_MS = 2000;

/** Where user keybindings survive a reload (implement with localStorage,
 *  a backend preference API, …). Specs are the same strings `bind` takes. */
export interface KeymapStore {
  load(): Record<string, string> | Promise<Record<string, string>>;
  save(userBindings: Record<string, string>): void | Promise<void>;
}

export interface BindingInfo {
  /** The spec currently in effect ('Ctrl+A', 'K K', …). */
  spec: string;
  /** True when the user set it (shown as “custom” in the palette). */
  isUser: boolean;
  /** True when the command is registered but currently unbound — its default
   *  combo was reserved by another command's user binding. */
  displaced?: boolean;
}

interface DefaultBinding {
  spec: string;
  when?: ContextPredicate;
  weight?: number;
}

export interface KeymapOptions {
  /** Extra context flags merged into every resolution. */
  context?: () => Record<string, unknown>;
}

export class KeymapService {
  readonly commands = new CommandRegistry();
  readonly focus = new FocusStack();
  private bindings: Keybinding[] = [];
  private defaults = new Map<string, DefaultBinding>();
  private users = new Map<string, string>();
  private store: KeymapStore | null = null;
  private listeners = new Set<() => void>();
  private chordPrefix: Keybinding[] | null = null;
  private chordTimer: ReturnType<typeof setTimeout> | null = null;
  private target: Document | HTMLElement = document;
  private handler: ((event: Event) => void) | null = null;
  private options: KeymapOptions;

  constructor(options: KeymapOptions = {}) {
    this.options = options;
  }

  /** Supply (or replace) extra context flags, e.g. `pickerOpen`, `inLedgerCell`.
   *  Hosts call this once at boot with a getter reading live state. */
  setContextProvider(context?: () => Record<string, unknown>): void {
    this.options = { ...this.options, context };
  }

  /** Subscribe to any binding change (palette live-updates). Returns unsubscribe. */
  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  // -------------------------------------------------------------- capture
  /** While capturing, *no* command matching happens — the next physical
   *  keydown belongs to the rebinding UI, not to any command (so defining
   *  Ctrl+A can never post a voucher). Hosts consume the raw event themselves. */
  private capturing = false;

  isCapturing(): boolean {
    return this.capturing;
  }

  beginCapture(): void {
    this.capturing = true;
  }

  endCapture(): void {
    this.capturing = false;
  }

  // ------------------------------------------------------------- commands
  registerCommand(command: Command): void {
    this.commands.register(command);
  }

  unregisterCommand(id: string): void {
    this.commands.unregister(id);
    this.defaults.delete(id);
    this.bindings = this.bindings.filter((b) => b.command !== id);
    this.emit();
  }

  /** Bind a key spec ('Ctrl+A', 'K K') to a command — the *default*
   *  registration screens make on mount. A user binding for the command
   *  wins; a default whose combo is reserved by another user binding is
   *  skipped (that combo belongs to the user's choice). */
  bind(spec: string, commandId: string, when?: ContextPredicate, weight?: number): void {
    if (!this.commands.get(commandId)) {
      throw new Error(`Cannot bind to unknown command: ${commandId}`);
    }
    this.defaults.set(commandId, { spec, when, weight });
    this.syncCommand(commandId);
    this.emit();
  }

  /** User rebinding: reserves `spec` for the command, displacing any other
   *  binding (default or user) on that combo. Persisted via the store. */
  setKeybinding(spec: string, commandId: string): void {
    if (!this.defaults.has(commandId)) {
      throw new Error(`Cannot rebind unknown or unbound command: ${commandId}`);
    }
    const combo = parseBindingSpec(spec); // throws on malformed specs
    this.users.set(commandId, spec);
    // a combo triggers exactly one command: drop other user bindings on it
    for (const [otherId, otherSpec] of [...this.users]) {
      if (otherId !== commandId && comboEquals(parseBindingSpec(otherSpec), combo)) {
        this.users.delete(otherId);
      }
    }
    this.syncAll();
    void this.persist();
    this.emit();
  }

  /** Forget the user binding; the command returns to its default. */
  resetKeybinding(commandId: string): void {
    if (this.users.delete(commandId)) {
      this.syncAll();
      void this.persist();
      this.emit();
    }
  }

  /** Restore the shipped map entirely. */
  resetAllKeybindings(): void {
    this.users.clear();
    this.syncAll();
    void this.persist();
    this.emit();
  }

  unbind(spec: string, commandId?: string): void {
    const combo = parseBindingSpec(spec);
    this.bindings = this.bindings.filter(
      (b) =>
        !(
          b.command === (commandId ?? b.command) &&
          b.combo.length === combo.length &&
          b.combo.every((c, i) => {
            const other = combo[i];
            return other !== undefined && comboMatches(c, other);
          })
        ),
    );
    this.emit();
  }

  /** Current bindings (palette / help view). */
  listBindings(): Keybinding[] {
    return [...this.bindings];
  }

  /** The spec in effect for a command, and whether the user set it.
   *  `displaced` marks a command whose default combo was reserved by
   *  another command's user binding — it is currently unbound. Null when
   *  the command is not registered (a pending user binding waits for it). */
  bindingInfo(commandId: string): BindingInfo | null {
    if (!this.commands.get(commandId)) return null;
    const userSpec = this.users.get(commandId);
    if (userSpec) return { spec: userSpec, isUser: true, displaced: false };
    const def = this.defaults.get(commandId);
    if (!def) return null;
    const bound = this.bindings.some((b) => b.command === commandId);
    return { spec: def.spec, isUser: false, displaced: !bound };
  }

  /** Snapshot of user bindings — what the store persists. */
  userBindings(): Record<string, string> {
    return Object.fromEntries(this.users);
  }

  // ------------------------------------------------------------ persistence
  /** Attach a store, load it, and keep it written on every change. Safe to
   *  call before commands register: loaded bindings apply when their command
   *  shows up. */
  async attachPersistence(store: KeymapStore): Promise<void> {
    this.store = store;
    let loaded: Record<string, string> = {};
    try {
      loaded = await store.load();
    } catch {
      loaded = {}; // a broken store must never break the keymap
    }
    for (const [commandId, spec] of Object.entries(loaded)) {
      if (typeof spec !== 'string') continue;
      try {
        parseBindingSpec(spec);
      } catch {
        continue; // skip junk rather than fail boot
      }
      this.users.set(commandId, spec);
    }
    this.syncAll();
    this.emit();
  }

  private persist(): void {
    if (!this.store) return;
    void Promise.resolve(this.store.save(this.userBindings())).catch(() => {
      /* persistence failures are non-fatal by design */
    });
  }

  // ------------------------------------------------------------ lifecycle
  start(target: Document | HTMLElement = document): void {
    this.stop();
    this.target = target;
    this.handler = (event: Event) => this.handleEvent(event as KeyboardEvent);
    target.addEventListener('keydown', this.handler, true); // capture phase
  }

  stop(): void {
    if (this.handler) {
      this.target.removeEventListener('keydown', this.handler, true);
      this.handler = null;
    }
    this.capturing = false;
    this.clearChord();
  }

  // ------------------------------------------------------------ resolution
  private context(): KeymapContext {
    return { ...this.focus.context(), ...this.options.context?.() };
  }

  private candidates(): Keybinding[] {
    if (this.chordPrefix) return this.chordPrefix;
    // chords always outrank single combos; then weight, then recency
    return [...this.bindings].sort(
      (a, b) =>
        (b.combo.length - a.combo.length) ||
        (b.weight ?? 0) - (a.weight ?? 0),
    );
  }

  private match(event: KeyboardEvent): Keybinding | null {
    const incoming = comboFromEvent(event);
    const ctx = this.context();
    for (const binding of this.candidates()) {
      const first = binding.combo[this.chordPrefix ? 1 : 0];
      if (!first || !comboMatches(first, incoming)) continue;
      // plain typing is never stolen: single-key bindings require a modifier
      // unless the user is not inside a text field.
      const needsModifier = binding.combo.length === 1 && first.key.length === 1;
      if (needsModifier && !first.ctrl && !first.meta && !first.alt && ctx.inputFocused) {
        continue;
      }
      if (binding.when && !binding.when(ctx)) continue;
      const command = this.commands.get(binding.command);
      if (!command || (command.when && !command.when(ctx))) continue;
      if (binding.combo.length > 1 && !this.chordPrefix) {
        this.beginChord(event, binding);
        return null;
      }
      return binding;
    }
    return null;
  }

  private beginChord(event: KeyboardEvent, first: Keybinding): void {
    event.preventDefault();
    const firstCombo = first.combo[0];
    const chordBindings = this.bindings.filter(
      (b) => b.combo.length > 1 && firstCombo !== undefined && b.combo[0] !== undefined
        && comboMatches(b.combo[0], firstCombo),
    );
    this.chordPrefix = chordBindings;
    this.chordTimer = setTimeout(() => this.clearChord(), CHORD_TIMEOUT_MS);
  }

  private clearChord(): void {
    if (this.chordTimer) clearTimeout(this.chordTimer);
    this.chordTimer = null;
    this.chordPrefix = null;
  }

  handleEvent(event: KeyboardEvent): boolean {
    if (this.capturing) return false; // the next key belongs to a rebind capture
    if (isComposing(event)) return false;
    if (['Control', 'Meta', 'Alt', 'Shift'].includes(event.key)) return false;

    const binding = this.match(event);
    if (binding) {
      event.preventDefault();
      event.stopPropagation();
      this.clearChord();
      void this.execute(binding.command);
      return true;
    }
    return false;
  }

  execute(commandId: string, args?: unknown): unknown {
    const command = this.commands.get(commandId);
    if (!command) throw new Error(`Unknown command: ${commandId}`);
    return command.run(args);
  }

  // ------------------------------------------------------------ sync layer
  /** Recompute the live binding of one command from defaults + user map. */
  private syncCommand(commandId: string): void {
    this.bindings = this.bindings.filter((b) => b.command !== commandId);
    const def = this.defaults.get(commandId);
    if (!def) return;
    const userSpec = this.users.get(commandId);
    const spec = userSpec ?? def.spec;
    if (!userSpec && this.isReserved(parseBindingSpec(spec), commandId)) {
      return; // combo belongs to another command's user binding — skip default
    }
    this.bindings.push({ combo: parseBindingSpec(spec), command: commandId, when: def.when, weight: def.weight });
  }

  private syncAll(): void {
    for (const commandId of this.defaults.keys()) this.syncCommand(commandId);
  }

  /** True when another command's *user* binding already owns this combo. */
  private isReserved(combo: KeyCombo[], ownerId: string): boolean {
    for (const [commandId, spec] of this.users) {
      if (commandId !== ownerId && comboEquals(parseBindingSpec(spec), combo)) return true;
    }
    return false;
  }
}

function comboEquals(a: KeyCombo[], b: KeyCombo[]): boolean {
  return (
    a.length === b.length &&
    a.every((c, i) => {
      const other = b[i];
      return other !== undefined && comboMatches(c, other) && comboMatches(other, c);
    })
  );
}
