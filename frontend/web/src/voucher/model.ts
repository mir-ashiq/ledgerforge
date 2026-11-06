/**
 * Voucher draft model — one Tally-style grid: Particulars | Debit | Credit |
 * Narration. A line is *complete* when it has a ledger and exactly one
 * positive amount; amounts stay raw strings until they hit the wire (Law 3).
 */

import type { Dc } from '../api/types';

/** The F-key switchable voucher types (seeded per book by ledgerforge/seeding.py). */
export interface VoucherTypeDef {
  code: string;
  name: string;
  key: string;
}

export const VOUCHER_TYPES: readonly VoucherTypeDef[] = [
  { code: 'CN', name: 'Contra', key: 'F4' },
  { code: 'PYT', name: 'Payment', key: 'F5' },
  { code: 'RCT', name: 'Receipt', key: 'F6' },
  { code: 'JV', name: 'Journal', key: 'F7' },
  { code: 'SAL', name: 'Sales', key: 'F8' },
  { code: 'PUR', name: 'Purchase', key: 'F9' },
];

export const DEFAULT_TYPE_CODE = 'SAL';

export function typeDef(code: string): VoucherTypeDef {
  return VOUCHER_TYPES.find((t) => t.code === code) ?? VOUCHER_TYPES[0]!;
}

export interface GridLine {
  key: number;
  ledgerId: number | null;
  ledgerName: string;
  /** Raw cell text; grouping commas allowed while typing. */
  debit: string;
  credit: string;
  narration: string;
}

export type CellCol = 'ledger' | 'debit' | 'credit' | 'narration';

export interface Cursor {
  row: number;
  col: CellCol;
}

export interface DraftState {
  typeCode: string;
  date: string; // YYYY-MM-DD
  lines: GridLine[];
  cursor: Cursor;
  clientToken: string;
}

export type LineStatus = 'empty' | 'complete' | 'incomplete';

export interface PreparedLine {
  ledgerId: number;
  ledgerName: string;
  dc: Dc;
  amount: string; // canonical decimal string
}

let keySeq = 0;

export function blankLine(): GridLine {
  keySeq += 1;
  return { key: keySeq, ledgerId: null, ledgerName: '', debit: '', credit: '', narration: '' };
}

export function newClientToken(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`; // test fallback, never in production browsers
}

/** Today in the ledger's home timezone (IST) — the books live there. */
export function todayIso(): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
  return parts; // en-CA yields YYYY-MM-DD
}

export function isValidIsoDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split('-').map(Number) as [number, number, number];
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

export function initDraft(typeCode = DEFAULT_TYPE_CODE, date = todayIso()): DraftState {
  return {
    typeCode,
    date,
    lines: [blankLine()],
    cursor: { row: 0, col: 'ledger' },
    clientToken: newClientToken(),
  };
}
