/**
 * Auto-balance HUD (docs/keyboard-ux.md §4): exact totals from decimal
 * string math — never floats — and the amount left to balance.
 */

import { formatEnIn, isZero } from '../money/exact';
import type { GridLine } from '../voucher/model';
import { computeTotals } from '../voucher/validate';

export function BalanceHud({ lines }: { lines: readonly GridLine[] }) {
  const totals = computeTotals(lines);
  const balanced = isZero(totals.difference);
  const left = totals.difference.neg
    ? { neg: false, int: totals.difference.int, scale: totals.difference.scale }
    : totals.difference;
  const hasEntries = totals.completeRows.length > 0 || totals.invalidRows.length > 0;

  return (
    <footer className="hud">
      <div className="hud-totals">
        <span className="hud-item">Debit <b>{formatEnIn(totals.debit)}</b></span>
        <span className="hud-item">Credit <b>{formatEnIn(totals.credit)}</b></span>
        <span className={`hud-item ${balanced ? 'ok' : 'left'}`}>
          {balanced
            ? (hasEntries ? 'Balanced' : 'Amount left 0.00')
            : `Amount left ${formatEnIn(left)}`}
        </span>
        {totals.invalidRows.length > 0 && (
          <span className="hud-item bad">
            Invalid amount on line {totals.invalidRows.map((r) => r + 1).join(', ')}
          </span>
        )}
      </div>
      <div className="hud-keys" aria-hidden="true">
        Ctrl+A accept · Ctrl+Enter fill balance · Alt+C create ledger · Alt+D delete line ·
        Alt+L bills · F2 date · Alt+N new · Ctrl+K keys
      </div>
    </footer>
  );
}
