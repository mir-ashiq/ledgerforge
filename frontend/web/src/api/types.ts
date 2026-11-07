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
  /** The debit-side total in book currency, engine-computed and quantized
   * to cents — the daybook's amount column (ADR-015). A string: the client
   * never sums (Law 3). */
  amount: string;
  narration: string | null;
  reversal_of: number | null;
  posted_at: string;
  client_token: string;
  replayed: boolean;
  /** Business-document payloads (ADR-010/012): invoice, stock, payroll… */
  meta?: Record<string, unknown> | null;
  entries: { line_no: number; ledger_id: number; ledger_name: string; dc: Dc; amount: string }[];
}

// ---- payroll documents (ADR-012) -------------------------------------------

export type PayrollComponentKind = 'earning' | 'withheld' | 'employer';

export interface PayrollComponent {
  name: string;
  amount: string;
  kind: PayrollComponentKind;
  /** Employer components book into this liability ledger. */
  liability?: string;
}

export interface PayrollRunEmployee {
  employee: string;
  components: PayrollComponent[];
}

export interface PayrollComputedEmployee {
  employee: string;
  gross: string;
  withheld: string;
  employer: string;
  net: string;
  ytd: { gross: string; withheld: string; employer: string; net: string; runs: number };
  components: Array<PayrollComponent & { ytd: string }>;
}

export interface PayrollRunDoc {
  mode: 'run';
  period: string;
  employees: PayrollRunEmployee[];
  computed?: {
    period: string;
    fy: string;
    employees: PayrollComputedEmployee[];
    total_net: string;
  };
}

/** The plugin inventory (ADR-016): what loaded, through which channel. */
export interface PluginOut {
  name: string;
  version: string;
  api_compat: string;
  /** "entry-point" = first-party pack or pip install (operator's channel);
   *  "drop-in" = LF_PLUGIN_PATH module that passed the trust gate. */
  source: 'entry-point' | 'drop-in';
  /** "trusted" = operator's own channel; "signed" = approved publisher's
   *  detached signature; "acknowledged" = operator-pinned bytes. */
  trust: 'trusted' | 'signed' | 'acknowledged';
}

/** A plugin (or drop-in directory) that did not load, and why. */
export interface PluginRefusalOut {
  module: string;
  reason: string;
}

export interface PluginsOut {
  plugins: PluginOut[];
  refused: PluginRefusalOut[];
}

/** One sandboxed UI slot a pack declares (ADR-019, plugin API 0.7). */
export interface SlotOut {
  key: string;
  title: string;
  plugin: string;
  source: 'entry-point' | 'drop-in';
  trust: 'trusted' | 'signed' | 'acknowledged';
  /** null = the host serves the document (inline HTML) at /api/v1/slots/{key};
   *  an http(s) URL the host embeds directly otherwise. */
  url: string | null;
}

export interface SlotsOut {
  slots: SlotOut[];
}

// ---- books (ADR-009) --------------------------------------------------------

/** One company (book): its chart of accounts, numbering, base currency. */
export interface BookOut {
  id: number;
  name: string;
  slug: string;
  base_currency: string;
  fy_start_month: number;
  is_archived: boolean;
  created_at: string;
}

/** What POST /books returns: the book plus proof of the TallyPrime seed. */
export interface BookSeededOut {
  book: BookOut;
  seeded_groups: number;
  seeded_ledgers: number;
  seeded_voucher_types: number;
}

export interface BookCreateInput {
  name: string;
  slug?: string;
  base_currency?: string;
  fy_start_month?: number;
}

export interface BookUpdateInput {
  name?: string;
  is_archived?: boolean;
}

// ---- periods (ADR-008) ------------------------------------------------------

export type PeriodStatus = 'open' | 'locked' | 'closed';

export interface PeriodOut {
  id: number;
  name: string;
  starts_on: string;
  ends_on: string;
  status: PeriodStatus;
  locked_at: string | null;
  closed_at: string | null;
  closed_by: string | null;
}

export interface PeriodCreateInput {
  name: string;
  starts_on: string;
  ends_on: string;
}

/** One zeroing line the close sweep would post (the close preview). */
export interface FyCloseLineOut {
  bucket: 'income' | 'expense' | 'profit_loss';
  ledger_id: number;
  ledger_name: string;
  dc: Dc;
  amount: string;
}

export interface FyCloseReportOut {
  as_of: string;
  lines: FyCloseLineOut[];
  net_profit: string;
  totals: Record<string, string>;
}

/** What POST /periods/{id}/close returns: the closed period and the sweep. */
export interface PeriodCloseResult {
  period: PeriodOut;
  sweep_voucher: VoucherOut | null;
  sweep_posted: boolean;
}

// ---- reversal (Law 2's one correction path) ----------------------------------

export interface ReversalPayload {
  reversal_date?: string;
  narration?: string;
  client_token?: string;
}
