/**
 * The type-ahead listbox used by ledger and group pickers. Purely
 * presentational: the owner holds the filter text and highlight index and
 * registers the Arrow/Enter commands; this component renders the list, pushes
 * its focus scope (so Escape closes it before anything else), and supports
 * mouse picks for free.
 */

import { useFocusScope } from '@ledgerforge/keyboard-kit/react';

export interface PickerEntry {
  id: number;
  name: string;
  /** Right-aligned context: group name for ledgers, nature for groups. */
  context: string;
}

interface LedgerPickerProps {
  /** Focus scope id — unique per open picker ('ledgerPicker' | 'groupPicker'). */
  scopeId: string;
  entries: readonly PickerEntry[];
  highlight: number;
  listboxId: string;
  onPick: (index: number) => void;
  onClose: () => void;
  emptyText: string;
  footerHint?: string;
}

export function LedgerPicker({
  scopeId,
  entries,
  highlight,
  listboxId,
  onPick,
  onClose,
  emptyText,
  footerHint,
}: LedgerPickerProps) {
  useFocusScope(scopeId, () => {
    onClose();
    return true;
  });

  return (
    <div className="picker" role="presentation">
      <div className="picker-list" role="listbox" id={listboxId} aria-label="Matches">
        {entries.length === 0 && <div className="picker-empty">{emptyText}</div>}
        {entries.map((entry, i) => (
          <div
            key={entry.id}
            id={`${listboxId}-opt-${i}`}
            role="option"
            aria-selected={i === highlight}
            className={i === highlight ? 'hl' : ''}
            // mousedown (not click) so the input never loses focus first
            onMouseDown={(e) => {
              e.preventDefault();
              onPick(i);
            }}
          >
            <span className="picker-name">{entry.name}</span>
            <span className="picker-ctx">{entry.context}</span>
          </div>
        ))}
      </div>
      <div className="picker-hint">{footerHint ?? 'Up/Down select · Enter accept · Esc close'}</div>
    </div>
  );
}
