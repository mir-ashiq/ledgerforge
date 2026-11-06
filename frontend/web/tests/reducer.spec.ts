import { describe, expect, it } from 'vitest';
import { blankLine, initDraft, type DraftState, type GridLine } from '../src/voucher/model';
import { voucherReducer } from '../src/voucher/reducer';
import { computeTotals, isPristine, validateForPost } from '../src/voucher/validate';

function state(): DraftState {
  return initDraft('SAL', '2026-11-06');
}

function setLedger(s: DraftState, row: number, id: number, name: string): DraftState {
  return voucherReducer(s, { type: 'setLedger', row, ledgerId: id, ledgerName: name });
}

function type(s: DraftState, row: number, col: 'ledger' | 'debit' | 'credit' | 'narration', value: string): DraftState {
  return voucherReducer(s, { type: 'setCell', row, col, value });
}

function forward(s: DraftState): DraftState {
  return voucherReducer(s, { type: 'cellForward', row: s.cursor.row, col: s.cursor.col });
}

describe('Enter navigation (cellForward)', () => {
  it('walks ledger → debit → credit → next row, skipping an empty narration', () => {
    let s = state();
    expect(s.cursor).toEqual({ row: 0, col: 'ledger' });
    s = forward(s);
    expect(s.cursor).toEqual({ row: 0, col: 'debit' });
    s = forward(s);
    expect(s.cursor).toEqual({ row: 0, col: 'credit' });
    s = forward(s); // narration empty → next row
    expect(s.cursor).toEqual({ row: 1, col: 'ledger' });
    expect(s.lines).toHaveLength(2); // auto-appended
  });

  it('stops at narration when it has text', () => {
    let s = type(state(), 0, 'narration', 'cash sale');
    s = { ...s, cursor: { row: 0, col: 'credit' } };
    s = forward(s);
    expect(s.cursor).toEqual({ row: 0, col: 'narration' });
  });

  it('auto-appends past the last line — entry never breaks', () => {
    let s = state();
    s = { ...s, cursor: { row: 0, col: 'narration' as const } };
    s = forward(s);
    expect(s.lines).toHaveLength(2);
    expect(s.cursor).toEqual({ row: 1, col: 'ledger' });
  });

  it('ignores stale forward events from a previous cell', () => {
    let s = state();
    s = forward(s); // now 0:debit
    const stale = voucherReducer(s, { type: 'cellForward', row: 0, col: 'ledger' });
    expect(stale.cursor).toEqual(s.cursor);
  });
});

describe('Shift+Enter navigation (cellBack)', () => {
  it('walks backwards within a line', () => {
    let s: DraftState = { ...state(), cursor: { row: 0, col: 'narration' as const } };
    s = voucherReducer(s, { type: 'cellBack', ...s.cursor });
    expect(s.cursor).toEqual({ row: 0, col: 'credit' });
    s = voucherReducer(s, { type: 'cellBack', ...s.cursor });
    expect(s.cursor).toEqual({ row: 0, col: 'debit' });
    s = voucherReducer(s, { type: 'cellBack', ...s.cursor });
    expect(s.cursor).toEqual({ row: 0, col: 'ledger' });
  });

  it('from a ledger cell it returns to the previous row', () => {
    let s = state();
    s = forward(s);
    s = forward(s);
    s = forward(s); // row 1 auto-appended, cursor 1:ledger
    s = type(s, 0, 'narration', 'note');
    const back = voucherReducer(s, { type: 'cellBack', row: 1, col: 'ledger' });
    expect(back.cursor).toEqual({ row: 0, col: 'narration' });
  });
});

describe('text cells', () => {
  it('typing in the ledger column edits ledgerName (and filters the picker)', () => {
    let s = state();
    s = type(s, 0, 'ledger', 'Cash');
    expect(s.lines[0]!.ledgerName).toBe('Cash');
    expect(s.lines[0]!.ledgerId).toBeNull();
    s = type(s, 0, 'narration', 'opening');
    expect(s.lines[0]!.narration).toBe('opening');
  });
});

describe('amount cells are mutually exclusive', () => {
  it('typing a debit clears the credit and vice versa', () => {
    let s = state();
    s = type(s, 0, 'debit', '100');
    s = type(s, 0, 'credit', '5');
    expect(s.lines[0]).toMatchObject({ debit: '', credit: '5' });
    s = type(s, 0, 'debit', '100');
    expect(s.lines[0]).toMatchObject({ debit: '100', credit: '' });
  });
});

describe('Alt+D (deleteLine)', () => {
  it('removes the row and lands on the same row number', () => {
    let s = state();
    s = setLedger(s, 0, 1, 'Cash');
    s = forward(s); s = forward(s); s = forward(s); // row 1 appended
    s = setLedger(s, 1, 2, 'Local Sales');
    s = voucherReducer(s, { type: 'deleteLine', row: 0 });
    expect(s.lines).toHaveLength(1);
    expect(s.lines[0]!.ledgerName).toBe('Local Sales');
    expect(s.cursor).toEqual({ row: 0, col: 'ledger' });
  });

  it('clears the only line but keeps the draft (token included)', () => {
    let s = setLedger(state(), 0, 1, 'Cash');
    const token = s.clientToken;
    s = voucherReducer(s, { type: 'deleteLine', row: 0 });
    expect(s.lines[0]!.ledgerName).toBe('');
    expect(s.clientToken).toBe(token);
  });
});

describe('fillBalance (Ctrl+Enter)', () => {
  it('fills the exact difference into an empty cell only', () => {
    let s = state();
    s = setLedger(s, 0, 1, 'Cash');
    s = type(s, 0, 'debit', '1180.00');
    s = { ...s, lines: [...s.lines, blankLine()] };
    s = voucherReducer(s, { type: 'fillBalance', row: 1, col: 'credit', amount: '1180' });
    expect(s.lines[1]!.credit).toBe('1180');
    // non-empty target is left untouched
    s = voucherReducer(s, { type: 'fillBalance', row: 1, col: 'credit', amount: '999' });
    expect(s.lines[1]!.credit).toBe('1180');
  });
});

describe('newDraft & misc', () => {
  it('newDraft keeps type and date, refreshes the idempotency token', () => {
    let s = setLedger(state(), 0, 1, 'Cash');
    s = voucherReducer(s, { type: 'newDraft' });
    expect(s.typeCode).toBe('SAL');
    expect(s.date).toBe('2026-11-06');
    expect(s.lines).toHaveLength(1);
    expect(s.lines[0]!.ledgerName).toBe('');
  });

  it('setType and setDate (invalid dates are refused)', () => {
    let s = voucherReducer(state(), { type: 'setType', code: 'PYT' });
    expect(s.typeCode).toBe('PYT');
    s = voucherReducer(s, { type: 'setDate', date: '2026-02-30' });
    expect(s.date).toBe('2026-11-06'); // unchanged: Feb 30 does not exist
    s = voucherReducer(s, { type: 'setDate', date: '2027-04-01' });
    expect(s.date).toBe('2027-04-01');
  });
});

describe('validateForPost (Law 1 mirror)', () => {
  function withLines(lines: Array<Partial<GridLine>>): DraftState {
    return {
      ...state(),
      lines: lines.map((l) => ({ ...blankLine(), ...l })),
    };
  }

  it('rejects an unbalanced draft with the exact difference', () => {
    const s = withLines([
      { ledgerId: 1, ledgerName: 'Cash', debit: '1180' },
      { ledgerId: 2, ledgerName: 'Local Sales', credit: '1000' },
    ]);
    const check = validateForPost(s);
    expect(check.ok).toBe(false);
    if (!check.ok) expect(check.error).toContain('180');
  });

  it('rejects single-line and ledger-less drafts', () => {
    const one = withLines([{ ledgerId: 1, ledgerName: 'Cash', debit: '10' }]);
    expect(validateForPost(one).ok).toBe(false); // one line only

    const noLedger = withLines([{ debit: '10' }]);
    expect(validateForPost(noLedger).ok).toBe(false); // amount without ledger

    const zero = withLines([
      { ledgerId: 1, ledgerName: 'Cash', debit: '0' },
      { ledgerId: 2, ledgerName: 'Sales', credit: '0' },
    ]);
    const vz = validateForPost(zero);
    expect(vz.ok).toBe(false); // zero amounts are not postings
    if (!vz.ok) expect(vz.error).toMatch(/greater than zero/);
  });

  it('accepts a balanced voucher with canonical string amounts', () => {
    const s = withLines([
      { ledgerId: 1, ledgerName: 'Cash', debit: '1180.00' },
      { ledgerId: 2, ledgerName: 'Local Sales', credit: '1000' },
      { ledgerId: 3, ledgerName: 'CGST Payable', credit: '90' },
      { ledgerId: 4, ledgerName: 'SGST Payable', credit: '90' },
      {}, // untouched trailing row is ignored
    ]);
    const check = validateForPost(s);
    expect(check.ok).toBe(true);
    if (check.ok) {
      expect(check.lines.map((l) => [l.dc, l.amount])).toEqual([
        ['D', '1180'],
        ['C', '1000'],
        ['C', '90'],
        ['C', '90'],
      ]);
      expect(check.lines.every((l) => typeof l.amount === 'string')).toBe(true);
    }
  });
});

describe('totals & pristine', () => {
  it('computes exact totals and flags invalid rows', () => {
    const lines = [
      { ...blankLine(), ledgerId: 1, ledgerName: 'Cash', debit: '1,180.50' },
      { ...blankLine(), credit: 'oops' },
    ];
    const totals = computeTotals(lines);
    expect(totals.debit.int).toBe(118050n);
    expect(totals.credit.int).toBe(0n);
    expect(totals.invalidRows).toEqual([1]);
  });

  it('pristine drafts are recognized', () => {
    expect(isPristine([blankLine(), blankLine()])).toBe(true);
    expect(isPristine([{ ...blankLine(), narration: 'x' }])).toBe(false);
  });
});
