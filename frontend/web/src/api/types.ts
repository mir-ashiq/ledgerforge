/** API DTOs — mirrors src/ledgerforge/schemas.py. Amounts are strings (Law 3). */

export interface LedgerOut {
  id: number;
  name: string;
  code: string | null;
  group_id: number;
  group_name: string;
  is_system: boolean;
}

export interface GroupOut {
  id: number;
  name: string;
  code: string | null;
  parent_id: number | null;
  nature: string;
  affects_gross: boolean;
  is_root: boolean;
}

export type Dc = 'D' | 'C';

export interface VoucherPostLine {
  ledger_id: number;
  dc: Dc;
  /** Decimal string — never a JSON number (Law 3). */
  amount: string;
}

export interface VoucherPostPayload {
  voucher_type: string;
  voucher_date: string;
  narration?: string;
  /** Idempotency key; resending a failed post replays safely. */
  client_token: string;
  entries: VoucherPostLine[];
}

export interface VoucherOut {
  id: number;
  uuid: string;
  voucher_no: string;
  voucher_type: string;
  voucher_date: string;
  narration: string | null;
  reversal_of: number | null;
  posted_at: string;
  client_token: string;
  replayed: boolean;
  entries: { line_no: number; ledger_id: number; ledger_name: string; dc: Dc; amount: string }[];
}
