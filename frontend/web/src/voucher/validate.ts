/**
 * Draft validation and exact totals — the client-side mirror of the engine's
 * Law 1 (debits = credits, exactly). The server remains the authority; these
 * checks give the typist instant, precise feedback instead of a 422.
 */

import type { VoucherPostLine } from '../api/types';
import { add, cmp, isZero, parseAmount, sub, toCanonical, type Exact } from '../money/exact';
import { isValidIsoDate, type DraftState, type GridLine } from './model';

export interface DraftTotals {
  debit: Exact;
  credit: Exact;
  /** debit - credit, signed. Zero means balanced. */
  difference: Exact;
  /** Rows whose non-empty amount text does not parse. */
  invalidRows: number[];
  /** Rows that would post (ledger + exactly one positive amount). */
  completeRows: number[];
}

export function computeTotals(lines: readonly GridLine[]): DraftTotals {
  let debit: Exact = { neg: false, int: 0n, scale: 0 };
  let credit: Exact = { neg: false, int: 0n, scale: 0 };
  const invalidRows: number[] = [];
  const completeRows: number[] = [];
  lines.forEach((line, row) => {
    const dr = parseAmount(line.debit);
    const cr = parseAmount(line.credit);
    if (!dr.ok && line.debit.trim() !== '') invalidRows.push(row);
    if (!cr.ok && line.credit.trim() !== '') invalidRows.push(row);
    if (dr.ok && dr.value.int > 0n) debit = add(debit, dr.value);
    if (cr.ok && cr.value.int > 0n) credit = add(credit, cr.value);
    const complete =
      line.ledgerId !== null &&
      ((dr.ok && dr.value.int > 0n && line.credit.trim() === '') ||
        (cr.ok && cr.value.int > 0n && line.debit.trim() === ''));
    if (complete) completeRows.push(row);
  });
  return { debit, credit, difference: sub(debit, credit), invalidRows, completeRows };
}

export type PostCheck =
  | { ok: true; lines: VoucherPostLine[] }
  | { ok: false; error: string };

/** Full pre-post validation; returns wire-ready lines with string amounts. */
export function validateForPost(state: DraftState): PostCheck {
  if (!isValidIsoDate(state.date)) return { ok: false, error: 'Voucher date is missing or invalid (F2 to change)' };

  const lines: VoucherPostLine[] = [];
  for (let row = 0; row < state.lines.length; row++) {
    const line = state.lines[row]!;
    const hasLedger = line.ledgerId !== null || line.ledgerName.trim() !== '';
    const hasAmount = line.debit.trim() !== '' || line.credit.trim() !== '';
    const hasText = hasLedger || hasAmount || line.narration.trim() !== '';
    if (!hasText) continue; // untouched auto-appended rows are ignored
    if (!hasLedger || !hasAmount) {
      return { ok: false, error: `Line ${row + 1}: needs both a ledger and an amount (Alt+D deletes it)` };
    }
    const dr = parseAmount(line.debit);
    const cr = parseAmount(line.credit);
    if (dr.ok && dr.value.int === 0n && line.debit.trim() !== '') {
      return { ok: false, error: `Line ${row + 1}: amount must be greater than zero` };
    }
    if (!dr.ok && line.debit.trim() !== '') return { ok: false, error: `Line ${row + 1}: ${dr.error}` };
    if (!cr.ok && line.credit.trim() !== '') return { ok: false, error: `Line ${row + 1}: ${cr.error}` };
    if (dr.ok && cr.ok && dr.value.int > 0n && cr.value.int > 0n) {
      return { ok: false, error: `Line ${row + 1}: a line is either debit or credit, not both` };
    }
    if (!line.ledgerId) {
      return { ok: false, error: `Line ${row + 1}: pick "${line.ledgerName}" from the list (Enter on the highlighted row)` };
    }
    const side = line.debit.trim() !== '' ? dr : cr;
    if (!side.ok) return { ok: false, error: `Line ${row + 1}: invalid amount` };
    lines.push({
      ledger_id: line.ledgerId,
      dc: line.debit.trim() !== '' ? 'D' : 'C',
      amount: toCanonical(side.value),
    });
  }

  if (lines.length < 2) {
    return { ok: false, error: 'A voucher needs at least two complete lines' };
  }
  const totals = computeTotals(state.lines);
  if (!isZero(totals.difference)) {
    const who = totals.difference.neg ? 'credits exceed debits' : 'debits exceed credits';
    return {
      ok: false,
      error: `Debits do not equal credits (${who} by ${toCanonical(abs(totals.difference))})`,
    };
  }
  return { ok: true, lines };
}

function abs(v: Exact): Exact {
  return { neg: false, int: v.int, scale: v.scale };
}

/** True when every line is empty — a pristine draft can be discarded freely. */
export function isPristine(lines: readonly GridLine[]): boolean {
  return lines.every(
    (l) => l.ledgerName.trim() === '' && l.debit.trim() === '' && l.credit.trim() === '' && l.narration.trim() === '',
  );
}

export { cmp };
