/**
 * Focus scope stack — the navigation half of the Tally contract:
 * Enter moves forward, Escape pops the deepest scope ("back").
 *
 * Scopes are pushed when a screen/region activates (e.g. opening the
 * ledger picker) and popped when it closes. The deepest scope wins
 * keybinding resolution; roving focus helpers drive ArrowUp/Down lists.
 */

import type { KeymapContext } from './types.js';

export interface FocusScope {
  /** Scope id, e.g. 'gateway' | 'voucherEditor' | 'ledgerPicker'. */
  id: string;
  /** Optional DOM container owning this scope's roving focus. */
  element?: HTMLElement;
  /**
   * Escape handler; return false to let the next scope handle it.
   * Defaults to popping this scope.
   */
  onEscape?: () => boolean;
  /** Elements participating in arrow-key roving focus (ordered). */
  rovingItems?: () => HTMLElement[];
}

const EDITABLE_SELECTOR = 'input, textarea, select, [contenteditable="true"]';

export class FocusStack {
  private stack: FocusScope[] = [];

  push(scope: FocusScope): void {
    this.stack.push(scope);
  }

  pop(): FocusScope | undefined {
    return this.stack.pop();
  }

  /** Remove a specific scope (component unmount / screen exit). */
  remove(scope: FocusScope): void {
    const i = this.stack.lastIndexOf(scope);
    if (i >= 0) this.stack.splice(i, 1);
  }

  current(): FocusScope | undefined {
    return this.stack[this.stack.length - 1];
  }

  /** Snapshot for keybinding resolution. */
  context(): KeymapContext {
    const element = document.activeElement;
    return {
      scopeId: this.current()?.id ?? null,
      scopeIds: [...this.stack].reverse().map((s) => s.id),
      inputFocused:
        element instanceof HTMLElement && !!element.closest(EDITABLE_SELECTOR),
    };
  }

  /**
   * Handle Escape: deepest scope first; a handler returning false defers
   * to the next scope. Returns true when consumed.
   */
  escape(): boolean {
    for (let i = this.stack.length - 1; i >= 0; i--) {
      const scope = this.stack[i];
      if (!scope) continue;
      if (!scope.onEscape || scope.onEscape() === false) {
        // default behavior: pop this scope and stop
        this.stack.splice(i, 1);
      }
      return true; // the deepest scope consumed the Escape
    }
    return false;
  }

  /** Move roving focus within the deepest scope that has items. */
  moveFocus(delta: number): boolean {
    for (let i = this.stack.length - 1; i >= 0; i--) {
      const scope = this.stack[i];
      if (!scope) continue;
      const items = scope.rovingItems?.();
      if (!items || items.length === 0) continue;
      const active = document.activeElement as HTMLElement | null;
      const current = items.findIndex((el) => el === active || el.contains(active));
      const next = items[(current + delta + items.length) % items.length];
      if (next) next.focus();
      return true;
    }
    return false;
  }
}
