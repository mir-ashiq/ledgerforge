/**
 * Daybook-screen logic (ADR-015): the browse over posted vouchers — what
 * document a voucher carried, display money, the client-side text filter.
 * Amounts stay strings end-to-end (Law 3); this module never computes.
 */

import type { VoucherOut } from '../api/types';
import { payrollRunOf } from '../payslip/model';

/** The business document a voucher carried, for the row's chip. */
export type DocKind = 'payroll-run' | 'payroll-payout' | 'invoice' | 'stock';

export function docKindOf(voucher: VoucherOut): DocKind | null {
  const meta = voucher.meta;
  if (!meta || typeof meta !== 'object') return null;
  const payroll = (meta as Record<string, unknown>).payroll;
  if (payroll && typeof payroll === 'object') {
    const mode = (payroll as Record<string, unknown>).mode;
    if (mode === 'run') return 'payroll-run';
    if (mode === 'payout') return 'payroll-payout';
  }
  if ('invoice' in meta) return 'invoice';
  if ('stock' in meta) return 'stock';
  return null;
}

/** The chip label for a document kind (null kinds show no chip). */
export const DOC_KIND_LABEL: Record<DocKind, string> = {
  'payroll-run': 'payroll',
  'payroll-payout': 'payout',
  invoice: 'invoice',
  stock: 'stock',
};

/**
 * A payroll run covering several employees needs `?employee=` to print
 * (the payslip subject, ADR-013); one-employee runs print directly. Null
 * for every other voucher — they print as themselves.
 */
export function printEmployeeArg(voucher: VoucherOut): string | null | 'many' {
  const doc = payrollRunOf(voucher);
  if (!doc) return null;
  const employees = doc.employees.map((e) => e.employee).filter((n) => n.trim() !== '');
  if (employees.length === 0) return null;
  if (employees.length === 1) return employees[0]!;
  return 'many';
}

/**
 * Display money for an exact posted string: storage keeps 8 decimals, so
 * `"100.00000000"` shows as `"100.00"` — but sub-cent precision is real and
 * shows exactly as posted. Formatting only, never arithmetic (Law 3).
 */
export function money(value: string | null | undefined): string {
  if (!value) return '—';
  const m = /^(-?)(\d+)\.(\d+)$/.exec(value);
  if (!m) return value;
  const [, sign, int, frac] = m;
  if (frac.length <= 2) return value;
  if (/^0*$/.test(frac.slice(2))) return `${sign}${int}.${frac.slice(0, 2)}`;
  return value;
}

/** The text filter: a voucher matches when any field carries the needle. */
export function matchesText(voucher: VoucherOut, needle: string): boolean {
  const q = needle.trim().toLowerCase();
  if (!q) return true;
  const haystack = [
    voucher.voucher_no,
    voucher.voucher_type,
    voucher.voucher_date,
    voucher.narration ?? '',
    ...voucher.entries.map((e) => e.ledger_name),
  ]
    .join(' ')
    .toLowerCase();
  return haystack.includes(q);
}
