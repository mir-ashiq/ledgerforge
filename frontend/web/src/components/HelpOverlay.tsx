/**
 * Keyboard reference overlay (Ctrl+K). Reads the live keymap, so it always
 * shows what is actually bound right now — the seed of the command palette
 * (docs/keyboard-ux.md §8, phase 3).
 */

import { formatBinding } from '@ledgerforge/keyboard-kit';
import { keymap, useFocusScope } from '@ledgerforge/keyboard-kit/react';

export function HelpOverlay({ onClose }: { onClose: () => void }) {
  useFocusScope('helpOverlay', () => {
    onClose();
    return true;
  });

  const rows = keymap.listBindings().map((binding) => ({
    keys: formatBinding(binding.combo),
    title: keymap.commands.get(binding.command)?.title ?? binding.command,
  }));

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card help" role="dialog" aria-label="Keyboard reference">
        <h2>Keyboard reference</h2>
        <div className="help-rows">
          {rows.map((r) => (
            <div className="help-row" key={r.keys + r.title}>
              <kbd>{r.keys}</kbd>
              <span>{r.title}</span>
            </div>
          ))}
        </div>
        <p className="overlay-foot">Bindings marked with a scope only fire there. Esc closes.</p>
      </div>
    </div>
  );
}
