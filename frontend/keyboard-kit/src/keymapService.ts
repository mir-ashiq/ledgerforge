/**
 * KeymapService — the single capture-phase keydown pipeline (ADR-004).
 *
 * One listener on the document sees every key before widget code:
 *   1. IME guard (never eat composition input)
 *   2. Build context from the focus scope stack
 *   3. Resolve: pending chord first, then bindings — chords beat singles,
 *      deeper scope weight wins, `when` predicates must pass
 *   4. On match: preventDefault + execute command; else let the browser type
 */

import { CommandRegistry } from './commandRegistry.js';
import { FocusStack } from './focusStack.js';
import {
  comboFromEvent,
  comboMatches,
  isComposing,
  parseBindingSpec,
} from './keybindings.js';
import type { Command, ContextPredicate, Keybinding, KeymapContext } from './types.js';

const CHORD_TIMEOUT_MS = 2000;

export interface KeymapOptions {
  /** Extra context flags merged into every resolution. */
  context?: () => Record<string, unknown>;
}

export class KeymapService {
  readonly commands = new CommandRegistry();
  readonly focus = new FocusStack();
  private bindings: Keybinding[] = [];
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

  // ------------------------------------------------------------- commands
  registerCommand(command: Command): void {
    this.commands.register(command);
  }

  unregisterCommand(id: string): void {
    this.commands.unregister(id);
    this.bindings = this.bindings.filter((b) => b.command !== id);
  }

  /** Bind a key spec ('Ctrl+A', 'K K') to a command. */
  bind(spec: string, commandId: string, when?: ContextPredicate, weight?: number): void {
    if (!this.commands.get(commandId)) {
      throw new Error(`Cannot bind to unknown command: ${commandId}`);
    }
    this.unbind(spec, commandId);
    this.bindings.push({ combo: parseBindingSpec(spec), command: commandId, when, weight });
  }

  /** User rebinding: replace every binding of a command with `spec`. */
  setKeybinding(spec: string, commandId: string): void {
    this.bindings = this.bindings.filter((b) => b.command !== commandId);
    this.bind(spec, commandId);
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
  }

  /** Current bindings (palette / help view). */
  listBindings(): Keybinding[] {
    return [...this.bindings];
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
}
