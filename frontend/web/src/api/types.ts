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
  /** Cost-centre splits (ADR-025): optional per line; when present, their
   * sum must equal ``amount`` exactly — a line is fully allocated or left
   * unallocated. */
  cost_allocations?: CostAllocationIn[];
}

/** One centre × amount split of a voucher line (ADR-025). */
export interface CostAllocationIn {
  centre_id: number;
  /** Decimal string — never a JSON number (Law 3). */
  amount: string;
}

/** A posted allocation as the engine returns it (centre resolved). */
export interface CostAllocationOut {
  centre_id: number;
  centre_name: string;
  dc: Dc;
  amount: string;
}

/** GET /cost-centres (ADR-025) — the allocation tree. */
export interface CostCentreOut {
  id: number;
  name: string;
  code: string | null;
  parent_id: number | null;
  category_id: number | null;
  category_name: string | null;
  is_root: boolean;
}

/** GET /cost-categories (ADR-025) — the classification axes. */
export interface CostCategoryOut {
  id: number;
  name: string;
  parent_id: number | null;
  is_root: boolean;
}

/** One centre row of the cost-centre summary (ADR-025) — the subtree rollup. */
export interface CostCentreSummaryRow {
  centre_id: number;
  centre_name: string;
  parent_id: number | null;
  is_root: boolean;
  category_id: number | null;
  category_name: string | null;
  dr: string;
  cr: string;
  net: string;
}

/** GET /reports/cost-centres?from&to (ADR-025). */
export interface CostCentreSummaryOut {
  from: string;
  to: string;
  centres: CostCentreSummaryRow[];
  unallocated: { dr: string; cr: string; net: string };
}

export interface VoucherPostPayload {
  voucher_type: string;
  voucher_date: string;
  narration?: string;
  /** Idempotency key; resending a failed post replays safely. */
  client_token: string;
  entries: VoucherPostLine[];
  /** Business-document payloads (ADR-010): the bills pack's allocations
   * (ADR-023) ride here alongside invoice/stock/payroll documents. */
  meta?: Record<string, unknown>;
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

// ---- bills: bill-wise & outstanding (ADR-023, plugin API 0.8) ----------------

/** One open bill of one party — a row of the `bills-open` pack report
 * (JSON, plugin API 0.8). Amounts are strings (Law 3). */
export interface OpenBill {
  ref: string;
  /** "dr" = the party owes you (receivable); "cr" = advance/payable. */
  side: 'dr' | 'cr';
  opened_on: string;
  due_on: string;
  outstanding: string; // signed: dr > 0, cr < 0
  bucket: string; // ageing bucket as of the report's as-of date
  interest_annual: string | null;
}

/** GET /reports/pack/bills-open?party=…&format=json */
export interface OpenBillsOut {
  party: string;
  as_of: string;
  bills: OpenBill[];
  on_account: string; // signed pool: > 0 the party owes, < 0 credit
}

/** One open bill row of the receivable/payable reports. */
export interface OutstandingBill extends OpenBill {
  party: string;
}

/** GET /reports/pack/bills-receivable|bills-payable?format=json */
export interface OutstandingReport {
  side: 'dr' | 'cr';
  as_of: string;
  party: string | null;
  bills: OutstandingBill[];
  on_account: Record<string, string>;
  party_totals: Record<string, string>;
  ageing: Record<string, string>;
  total: string;
}

/** A bill allocation ref the voucher screen's dialog builds (ADR-023):
 * dr/cr exactly like ledger lines — TallyPrime's bill-wise columns. */
export interface BillRef {
  kind: 'new' | 'against' | 'on_account';
  ref?: string;
  dr?: string;
  cr?: string;
  due_days?: number;
  due_on?: string;
  interest?: { annual: string; grace_days?: number };
}

export interface BillsDoc {
  party: string;
  refs: BillRef[];
  default_interest?: { annual: string; grace_days?: number };
}

// ---- banking: statements, matches, the proof (ADR-026, Phase 5) -----------
/** GET /banking/ledgers — the pickers' feed (balance is Cr-positive). */
export interface BankLedgerOut {
  id: number;
  name: string;
  balance: string;
}

/** POST /banking/{id}/statements — the receipt the UIs show and the
 * bank.statement.imported event carries. */
export interface BankImportReceipt {
  ledger_id: number;
  ledger_name: string;
  format: 'csv' | 'mt940';
  source: string | null;
  parsed: number;
  informational: number;
  new: number;
  duplicates: number;
  currency: string;
  opening_balance: string | null;
  closing_balance: string | null;
  dry_run: boolean;
  batch_id?: number;
}

/** The live (or last) match of one statement line. */
export interface BankMatchInfo {
  voucher_id: number;
  voucher_no: string;
  voucher_date: string;
  entry_id: number;
  matched_at: string;
  matched_by: string | null;
  unmatched_at: string | null;
  live: boolean;
}

/** One imported statement line — amounts are strings (Law 3). */
export interface BankLine {
  id: number;
  batch_id: number;
  value_date: string;
  dc: 'D' | 'C';
  amount: string;
  reference: string | null;
  narrative: string | null;
  cheque_no: string | null;
  match: BankMatchInfo | null;
}

/** GET /banking/{id}/lines */
export interface BankLineBook {
  ledger_id: number;
  ledger_name: string;
  status: string;
  lines: BankLine[];
}

/** One bank-ledger entry with its live-match state. */
export interface BankEntry {
  entry_id: number;
  voucher_id: number;
  voucher_no: string;
  voucher_date: string;
  dc: 'D' | 'C';
  amount: string;
  narration: string | null;
  matched: boolean;
}

/** GET /banking/{id}/entries */
export interface BankEntryBook {
  ledger_id: number;
  ledger_name: string;
  status: string;
  entries: BankEntry[];
}

/** One ranked candidate of the matchmaker (computed, never committed). */
export interface BankSuggestion {
  entry_id: number;
  voucher_id: number;
  voucher_no: string;
  voucher_date: string;
  dc: 'D' | 'C';
  amount: string;
  narration: string | null;
  score: number;
  reasons: string[];
}

/** GET /banking/{id}/suggestions */
export interface BankSuggestions {
  ledger_id: number;
  window_days: number;
  lines: {
    statement_line_id: number;
    value_date: string;
    dc: 'D' | 'C';
    amount: string;
    reference: string | null;
    narrative: string | null;
    cheque_no: string | null;
    suggestions: BankSuggestion[];
  }[];
}

/** POST /banking/{id}/matches */
export interface BankMatchResult {
  statement_line_id: number;
  voucher_id: number;
  voucher_no: string;
  entry_id: number;
  voucher_date: string;
  matched_by: string | null;
}

/** One row of the proof — an unmatched item, a pair difference, or one of
 * the statement-side baselines (the opening, the unexplained mass). */
export interface BankReconRow {
  kind: string;
  label: string;
  date: string;
  amount: string;
  effect: string;
  reference: string | null;
  narration: string | null;
}

/** GET /banking/{id}/reconciliation — the proof, exact strings. */
export interface BankReconciliation {
  ledger_id: number;
  ledger_name: string;
  as_of: string;
  book_balance: string;
  statement_balance: string;
  statement_balance_source: string;
  reconciled_balance: string;
  unmatched_statement_total: string;
  unmatched_book_total: string;
  pair_difference_total: string;
  rows: BankReconRow[];
  proof: { lhs: string; rhs: string; ok: boolean };
}

/** One cheque of the register — the derived lifecycle. */
export interface BankChequeRow {
  bank_ledger: string;
  no: string;
  voucher_no: string;
  voucher_date: string;
  issued_on: string;
  matures_on: string | null;
  amount: string | null;
  status: 'issued' | 'presented' | 'cleared';
  presented_on: string | null;
  cleared_by: string | null;
  pdc: boolean;
  matured_as_of: boolean;
}

/** GET /reports/pack/banking-cheques?format=json */
export interface BankChequeRegister {
  as_of: string;
  ledger: string | null;
  status: string | null;
  counts: { issued: number; presented: number; cleared: number };
  rows: BankChequeRow[];
}
