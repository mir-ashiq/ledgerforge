/**
 * The voucher line grid — TallyPrime's double-entry layout:
 * Particulars | Debit | Credit | Narration.
 *
 * Rows are absolutely positioned at a fixed height inside a spacer, so only
 * the visible window (plus overscan, plus the focused row) is rendered —
 * opening balances with hundreds of lines stay instant. Focus is real DOM
 * focus; the screen moves it via data-cell coordinates.
 */

import { useState } from 'react';
import type { LedgerOut } from '../api/types';
import { parseAmount } from '../money/exact';
import type { CellCol, DraftState } from '../voucher/model';
import { LedgerPicker, type PickerEntry } from './LedgerPicker';

export const ROW_HEIGHT = 34;
const OVERSCAN = 5;
const RENDER_WINDOW = 26;

interface VoucherGridProps {
  draft: DraftState;
  ledgers: readonly LedgerOut[];
  pickerOpen: boolean;
  pickerEntries: readonly PickerEntry[];
  pickerHighlight: number;
  onPickerPick: (index: number) => void;
  onPickerClose: () => void;
  onCellChange: (row: number, col: CellCol, value: string) => void;
  onFocusCell: (row: number, col: CellCol) => void;
}

function amountClass(raw: string): string {
  if (raw.trim() === '') return '';
  return parseAmount(raw).ok ? '' : 'bad';
}

export function VoucherGrid({
  draft,
  ledgers,
  pickerOpen,
  pickerEntries,
  pickerHighlight,
  onPickerPick,
  onPickerClose,
  onCellChange,
  onFocusCell,
}: VoucherGridProps) {
  const [scrollTop, setScrollTop] = useState(0);
  const total = draft.lines.length;
  const cursor = draft.cursor;

  let first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  let last = Math.min(total, first + RENDER_WINDOW);
  if (cursor.row < first) first = cursor.row;
  if (cursor.row >= last) last = cursor.row + 1;
  const rows = draft.lines.slice(first, last);
  const pickerRow = pickerOpen && cursor.col === 'ledger' ? cursor.row : -1;

  return (
    <div className="grid" role="grid" aria-rowcount={total} aria-label="Voucher lines">
      <div className="grid-head" role="row">
        <span className="c-no" role="columnheader">#</span>
        <span className="c-part" role="columnheader">Particulars</span>
        <span className="c-amt" role="columnheader">Debit</span>
        <span className="c-amt" role="columnheader">Credit</span>
        <span className="c-nar" role="columnheader">Narration</span>
      </div>
      <div
        className="grid-body"
        role="rowgroup"
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      >
        <div style={{ height: total * ROW_HEIGHT, position: 'relative' }}>
          {rows.map((line, i) => {
            const row = first + i;
            const showPicker = pickerRow === row;
            return (
              <div
                className="grid-row"
                role="row"
                key={line.key}
                style={{ position: 'absolute', top: row * ROW_HEIGHT, height: ROW_HEIGHT, left: 0, right: 0 }}
              >
                <span className="c-no" role="gridcell">{row + 1}</span>
                <span className="c-part cell" role="gridcell">
                  <input
                    data-cell={`${row}:ledger`}
                    value={line.ledgerName}
                    role="combobox"
                    aria-expanded={showPicker}
                    aria-controls={`picker-row-${row}`}
                    aria-autocomplete="list"
                    aria-label={`Line ${row + 1} particulars`}
                    onChange={(e) => onCellChange(row, 'ledger', e.target.value)}
                    onFocus={() => onFocusCell(row, 'ledger')}
                  />
                  {showPicker && (
                    <LedgerPicker
                      scopeId="ledgerPicker"
                      listboxId={`picker-row-${row}`}
                      entries={pickerEntries}
                      highlight={pickerHighlight}
                      onPick={onPickerPick}
                      onClose={onPickerClose}
                      emptyText={
                        ledgers.length === 0
                          ? 'No ledgers loaded — is the engine running?'
                          : 'No ledger matches — Alt+C creates it'
                      }
                      footerHint="Up/Down select · Enter accept · Alt+C create · Esc close"
                    />
                  )}
                </span>
                <span className="c-amt cell" role="gridcell">
                  <input
                    data-cell={`${row}:debit`}
                    value={line.debit}
                    inputMode="decimal"
                    className={amountClass(line.debit)}
                    aria-label={`Line ${row + 1} debit`}
                    placeholder="0.00"
                    onChange={(e) => onCellChange(row, 'debit', e.target.value)}
                    onFocus={() => onFocusCell(row, 'debit')}
                  />
                </span>
                <span className="c-amt cell" role="gridcell">
                  <input
                    data-cell={`${row}:credit`}
                    value={line.credit}
                    inputMode="decimal"
                    className={amountClass(line.credit)}
                    aria-label={`Line ${row + 1} credit`}
                    placeholder="0.00"
                    onChange={(e) => onCellChange(row, 'credit', e.target.value)}
                    onFocus={() => onFocusCell(row, 'credit')}
                  />
                </span>
                <span className="c-nar cell" role="gridcell">
                  <input
                    data-cell={`${row}:narration`}
                    value={line.narration}
                    aria-label={`Line ${row + 1} narration`}
                    onChange={(e) => onCellChange(row, 'narration', e.target.value)}
                    onFocus={() => onFocusCell(row, 'narration')}
                  />
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
