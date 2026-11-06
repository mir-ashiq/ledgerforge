/**
 * Pure reducer for the voucher draft. Enter (cellForward) walks a line
 * ledger -> debit -> credit -> narration, skipping an empty narration, and
 * auto-appends a row past the last line — entry never breaks
 * (docs/keyboard-ux.md §4). Debit and credit are mutually exclusive per
 * line, exactly like TallyPrime's double-entry grid.
 */

import { isValidIsoDate } from './model';
import {
  blankLine,
  type CellCol,
  type DraftState,
  type GridLine,
  newClientToken,
} from './model';

export type VoucherAction =
  | { type: 'setLedger'; row: number; ledgerId: number; ledgerName: string }
  | { type: 'setCell'; row: number; col: CellCol; value: string }
  | { type: 'cursor'; row: number; col: CellCol }
  | { type: 'cellForward'; row: number; col: CellCol }
  | { type: 'cellBack'; row: number; col: CellCol }
  | { type: 'deleteLine'; row: number }
  | { type: 'setType'; code: string }
  | { type: 'setDate'; date: string }
  | { type: 'fillBalance'; row: number; col: 'debit' | 'credit'; amount: string }
  | { type: 'newDraft' };

function withLines(state: DraftState, lines: GridLine[]): DraftState {
  return { ...state, lines };
}

function updateLine(state: DraftState, row: number, patch: (line: GridLine) => GridLine): DraftState {
  if (row < 0 || row >= state.lines.length) return state;
  const lines = state.lines.map((line, i) => (i === row ? patch(line) : line));
  return withLines(state, lines);
}

export function voucherReducer(state: DraftState, action: VoucherAction): DraftState {
  switch (action.type) {
    case 'setLedger':
      return updateLine(state, action.row, (line) => ({
        ...line,
        ledgerId: action.ledgerId,
        ledgerName: action.ledgerName,
      }));

    case 'setCell': {
      // the 'ledger' column edits ledgerName (the field and the column have
      // different names — the one place the mapping is not mechanical)
      if (action.col === 'ledger') {
        return updateLine(state, action.row, (line) => ({ ...line, ledgerName: action.value }));
      }
      if (action.col === 'narration') {
        return updateLine(state, action.row, (line) => ({ ...line, narration: action.value }));
      }
      // amount cells: typing on one side clears the other — one amount per line
      return updateLine(state, action.row, (line) =>
        action.col === 'debit'
          ? { ...line, debit: action.value, credit: action.value !== '' ? '' : line.credit }
          : { ...line, credit: action.value, debit: action.value !== '' ? '' : line.debit },
      );
    }

    case 'cursor':
      return { ...state, cursor: { row: action.row, col: action.col } };

    case 'cellForward': {
      const { row, col } = action;
      if (row !== state.cursor.row || col !== state.cursor.col) return state; // stale event
      const line = state.lines[row];
      if (!line) return state;
      if (col === 'ledger') return { ...state, cursor: { row, col: 'debit' } };
      if (col === 'debit') return { ...state, cursor: { row, col: 'credit' } };
      if (col === 'credit' && line.narration.trim() !== '') {
        return { ...state, cursor: { row, col: 'narration' } };
      }
      // from narration, or from credit with an empty narration: next line
      return advanceToNextRow(state, row);
    }

    case 'cellBack': {
      const { row, col } = action;
      if (col === 'debit') return { ...state, cursor: { row, col: 'ledger' } };
      if (col === 'credit') return { ...state, cursor: { row, col: 'debit' } };
      if (col === 'narration') return { ...state, cursor: { row, col: 'credit' } };
      if (row > 0) {
        const prev = state.lines[row - 1]!;
        const col: CellCol = prev.narration.trim() !== '' ? 'narration' : 'credit';
        return { ...state, cursor: { row: row - 1, col } };
      }
      return state; // already at the top of the grid
    }

    case 'deleteLine': {
      if (state.lines.length === 1) {
        // clearing the only line blanks the grid body; the draft (and its
        // idempotency token) survives
        const lines = [blankLine()];
        return { ...state, lines, cursor: { row: 0, col: 'ledger' } };
      }
      const lines = state.lines.filter((_, i) => i !== action.row);
      const row = Math.min(action.row, lines.length - 1);
      return { ...state, lines, cursor: { row, col: 'ledger' } };
    }

    case 'setType':
      return { ...state, typeCode: action.code };

    case 'setDate':
      return isValidIsoDate(action.date) ? { ...state, date: action.date } : state;

    case 'fillBalance': {
      const line = state.lines[action.row];
      if (!line) return state;
      const current = action.col === 'debit' ? line.debit : line.credit;
      if (current !== '') return state; // fill only into an empty cell
      return updateLine(state, action.row, (l) =>
        action.col === 'debit' ? { ...l, debit: action.amount } : { ...l, credit: action.amount },
      );
    }

    case 'newDraft':
      return initKeepMeta(state);

    default:
      return state;
  }
}

function advanceToNextRow(state: DraftState, row: number): DraftState {
  if (row + 1 < state.lines.length) {
    return { ...state, cursor: { row: row + 1, col: 'ledger' } };
  }
  // auto-append: Enter past the last cell of the last line grows the grid
  const line = blankLine();
  return {
    ...state,
    lines: [...state.lines, line],
    cursor: { row: state.lines.length, col: 'ledger' },
  };
}

/** Fresh grid, fresh idempotency token; keeps date and voucher type. */
function initKeepMeta(state: DraftState): DraftState {
  return {
    typeCode: state.typeCode,
    date: state.date,
    lines: [blankLine()],
    cursor: { row: 0, col: 'ledger' },
    clientToken: newClientToken(),
  };
}
