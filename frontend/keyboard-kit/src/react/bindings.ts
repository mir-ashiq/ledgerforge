/**
 * React bindings for keyboard-kit — thin adapters; the core stays
 * framework-agnostic (ADR-004). Requires react >= 18 as a peer.
 */
import { useEffect, useRef } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { KeymapService } from '../keymapService.js';
import type { Command, ContextPredicate } from '../types.js';

/** App-wide singleton. Host apps call `keymap.start()` once at boot. */
export const keymap = new KeymapService();

/** Register a command (and optional default binding) for a component's lifetime. */
export function useCommand(
  command: Command,
  binding?: string,
  when?: ContextPredicate,
): void {
  const runRef = useRef(command.run);
  runRef.current = command.run;

  useEffect(() => {
    keymap.registerCommand({ ...command, run: (args) => runRef.current(args) });
    if (binding) keymap.bind(binding, command.id, when);
    return () => keymap.unregisterCommand(command.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command.id, binding]);
}

/** Roving focus across `count` items, Tally-style: ArrowUp/Down moves,
 *  Enter activates. Returns a key handler the host attaches to items. */
export function useRovingFocus(
  count: number,
  onEnter: (index: number) => void,
): {
  onKey: (index: number, event: ReactKeyboardEvent) => void;
} {
  const activeRef = useRef(0);
  return {
    onKey(index: number, event: ReactKeyboardEvent) {
      activeRef.current = index;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + count) % count;
        activeRef.current = next;
        // hosts may listen for this event to update their highlight state
        document.dispatchEvent(
          new CustomEvent('lf:roving-move', { detail: { index: next } }),
        );
      } else if (event.key === 'Enter') {
        event.preventDefault();
        onEnter(activeRef.current);
      }
    },
  };
}

/**
 * Declare a focus scope for a screen region. Escape pops it (the universal
 * "back"); the scope id becomes available to `when` predicates.
 */
export function useFocusScope(id: string, onEscape?: () => boolean): void {
  useEffect(() => {
    keymap.focus.push({ id, onEscape });
    return () => {
      keymap.focus.pop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
}
