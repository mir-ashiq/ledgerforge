/**
 * Thin fetch client for the engine API. Relative URLs: the dev server and
 * the production container both proxy /api to the engine, so the browser
 * origin is the only origin involved.
 */

import type {
  BankChequeRegister,
  BankEntryBook,
  BankImportReceipt,
  BankLedgerOut,
  BankLineBook,
  BankMatchResult,
  BankReconciliation,
  BankSuggestions,
  BookCreateInput,
  BookOut,
  BookSeededOut,
  BookUpdateInput,
  CostCategoryOut,
  CostCentreOut,
  CostCentreSummaryOut,
  FyCloseReportOut,
  GroupOut,
  LedgerOut,
  OpenBillsOut,
  OutstandingReport,
  PeriodCloseResult,
  PeriodCreateInput,
  PeriodOut,
  PluginsOut,
  ReversalPayload,
  SlotsOut,
  StockBom,
  StockItemMaster,
  StockPriceRow,
  VoucherOut,
  VoucherPostPayload,
} from './types';

const BASE = '/api/v1';

/** The header every book-scoped route resolves the selected book from
 * (ADR-009 — TallyPrime's "select company", made a header). */
const BOOK_HEADER = 'X-Book-Id';

let activeBookId: number | null = null;

/** Select the book every request acts on; null falls back to the server's
 * single-active-book auto-resolution (the long-standing single-book mode). */
export function setActiveBook(id: number | null): void {
  activeBookId = id;
}

export function getActiveBookId(): number | null {
  return activeBookId;
}

function requestHeaders(extra?: HeadersInit): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (extra) Object.assign(headers, extra as Record<string, string>);
  if (activeBookId !== null) headers[BOOK_HEADER] = String(activeBookId);
  return headers;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: requestHeaders(init?.headers),
    });
  } catch {
    throw new ApiError(`Cannot reach the engine at ${BASE} — is it running? (make dev)`, 0);
  }
  if (!response.ok) {
    throw new ApiError(await errorDetail(response), response.status);
  }
  return (await response.json()) as T;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (typeof d === 'object' && d !== null && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join('; ');
    }
    if (detail && typeof detail === 'object' && 'msg' in detail) {
      return String((detail as { msg: unknown }).msg);
    }
  } catch {
    /* fall through */
  }
  return `Request failed (HTTP ${response.status})`;
}

async function requestBlob(path: string): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { headers: requestHeaders() });
  } catch {
    throw new ApiError(`Cannot reach the engine at ${BASE} — is it running? (make dev)`, 0);
  }
  if (!response.ok) {
    throw new ApiError(await errorDetail(response), response.status);
  }
  return response.blob();
}

export interface CreateLedgerInput {
  name: string;
  group_name: string;
}

/** Optional browse filters for the voucher list (the daybook, ADR-015). */
export interface VoucherQuery {
  /** Voucher type code (`SAL`, `PYT`…); empty = all types. */
  type?: string;
  /** Inclusive `YYYY-MM-DD` bounds on the voucher date. */
  from?: string;
  to?: string;
  limit?: number;
}

export const api = {
  ledgers: () => request<LedgerOut[]>('/ledgers'),
  groups: () => request<GroupOut[]>('/groups'),
  createLedger: (input: CreateLedgerInput) =>
    request<LedgerOut>('/ledgers', { method: 'POST', body: JSON.stringify(input) }),
  postVoucher: (payload: VoucherPostPayload) =>
    request<VoucherOut>('/vouchers', { method: 'POST', body: JSON.stringify(payload) }),
  /** Posted vouchers, newest first — the payslip screen filters payroll runs
   * from the bare list; the daybook passes the browse filters (ADR-015). */
  vouchers: (query: VoucherQuery = {}) => {
    const params = new URLSearchParams();
    if (query.type) params.set('type', query.type);
    if (query.from) params.set('date_from', query.from);
    if (query.to) params.set('date_to', query.to);
    params.set('limit', String(query.limit ?? 200));
    return request<VoucherOut[]>(`/vouchers?${params.toString()}`);
  },
  /** A voucher as its PDF (ADR-013): payslip for payroll runs with
   * `employee`, tax invoice for invoices, voucher slip otherwise — every
   * posting voucher prints, whatever carried it. */
  voucherPdf: (voucherId: number, employee?: string): Promise<Blob> =>
    requestBlob(
      employee
        ? `/vouchers/${voucherId}/pdf?employee=${encodeURIComponent(employee)}`
        : `/vouchers/${voucherId}/pdf`,
    ),
  /** The plugin inventory (ADR-016): loaded packs with their channel and
   *  trust basis, plus everything refused at boot and why. */
  plugins: () => request<PluginsOut>('/plugins'),
  /** Every UI slot a loaded pack declares (ADR-019) — the host's slot
   *  registry, one palette-reachable dashboard per entry. */
  slots: () => request<SlotsOut>('/slots'),
  /** Every book, archived last — the picker list (ADR-009). */
  books: () => request<BookOut[]>('/books'),
  /** Create a book and seed its chart of accounts in one action —
   *  TallyPrime's "create company" (admin role). */
  createBook: (input: BookCreateInput) =>
    request<BookSeededOut>('/books', { method: 'POST', body: JSON.stringify(input) }),
  /** Rename / archive a book (admin role). */
  updateBook: (bookId: number, patch: BookUpdateInput) =>
    request<BookOut>(`/books/${bookId}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  /** Post the mirrored reversal of a voucher — Law 2's one correction
   *  path, idempotent on retry. */
  reverseVoucher: (voucherId: number, payload: ReversalPayload = {}) =>
    request<VoucherOut>(`/vouchers/${voucherId}/reverse`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /** The book's fiscal periods, earliest first (ADR-008). */
  periods: () => request<PeriodOut[]>('/periods'),
  /** Create a period (admin role); overlapping ranges are refused. */
  createPeriod: (input: PeriodCreateInput) =>
    request<PeriodOut>('/periods', { method: 'POST', body: JSON.stringify(input) }),
  /** Freeze a period's dates for review; postings into it get 409. */
  lockPeriod: (periodId: number) =>
    request<PeriodOut>(`/periods/${periodId}/lock`, { method: 'POST' }),
  /** Reopen a locked period; refused once a close sweep exists. */
  unlockPeriod: (periodId: number) =>
    request<PeriodOut>(`/periods/${periodId}/unlock`, { method: 'POST' }),
  /** Exactly what close would post — the sweep preview (reader role). */
  closePreviewPeriod: (periodId: number) =>
    request<FyCloseReportOut>(`/periods/${periodId}/close-preview`),
  /** Lock, then close: posts the FY sweep and marks the period closed —
   *  final (admin role). */
  closePeriod: (periodId: number) =>
    request<PeriodCloseResult>(`/periods/${periodId}/close`, { method: 'POST' }),
  /** One party's open bills + on-account — the allocation dialog's feed
   * (ADR-023, plugin API 0.8: pack reports answer JSON). */
  openBills: (party: string) =>
    request<OpenBillsOut>(
      `/reports/pack/bills-open?party=${encodeURIComponent(party)}&format=json`,
    ),
  /** The cost trees (ADR-025): centres and their categories — the Alt+K
   *  split dialog's feed. */
  costCentres: () => request<CostCentreOut[]>('/cost-centres'),
  costCategories: () => request<CostCategoryOut[]>('/cost-categories'),
  createCostCentre: (input: { name: string; parent_name: string; category_name: string }) =>
    request<CostCentreOut>('/cost-centres', { method: 'POST', body: JSON.stringify(input) }),
  createCostCategory: (input: { name: string; parent_name: string }) =>
    request<CostCategoryOut>('/cost-categories', { method: 'POST', body: JSON.stringify(input) }),
  /** The centre rollup over a window (ADR-025) — centres + the Unallocated
   *  mass, allocated + unallocated = the window's whole entry mass. */
  costCentreSummary: (from: string, to: string) =>
    request<CostCentreSummaryOut>(
      `/reports/cost-centres?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
    ),
  /** The outstanding book with ageing — receivable ('dr') or payable ('cr'),
   * optionally one party (ADR-023). */
  outstanding: (side: 'dr' | 'cr', party?: string) => {
    const params = new URLSearchParams({ format: 'json' });
    if (party) params.set('party', party);
    const key = side === 'dr' ? 'bills-receivable' : 'bills-payable';
    return request<OutstandingReport>(`/reports/pack/${key}?${params.toString()}`);
  },

  // ---- inventory: stock masters + the depth folds (ADR-027) --------------
  /** Every stock-item master of the book, name order. */
  inventoryItems: (): Promise<StockItemMaster[]> => request('/inventory/items'),

  /** Create a stock-item master (tracking modes, re-order levels). */
  inventoryCreateItem: (input: Partial<StockItemMaster>) =>
    request<StockItemMaster>('/inventory/items', { method: 'POST', body: JSON.stringify(input) }),

  /** Revise a master (partial — unmentioned fields stand). */
  inventoryUpdateItem: (id: number, input: Partial<StockItemMaster>) =>
    request<StockItemMaster>(`/inventory/items/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(input),
    }),

  /** Delete a master (prices cascade; BOM references refuse). */
  inventoryDeleteItem: (id: number) =>
    request<{ deleted: boolean }>(`/inventory/items/${id}`, { method: 'DELETE' }),

  /** Price rows of the book — optionally one item's. */
  inventoryPrices: (itemId?: number): Promise<StockPriceRow[]> =>
    request(`/inventory/prices${itemId != null ? `?item_id=${itemId}` : ''}`),

  /** One date-effective price row (party null = the list price). */
  inventoryCreatePrice: (input: {
    item_id: number;
    party_ledger_id?: number | null;
    effective_from: string;
    rate: string;
  }) =>
    request<StockPriceRow>('/inventory/prices', {
      method: 'POST',
      body: JSON.stringify(input),
    }),

  inventoryDeletePrice: (id: number) =>
    request<{ deleted: boolean }>(`/inventory/prices/${id}`, { method: 'DELETE' }),

  /** Every BOM of the book with its components. */
  inventoryBoms: (): Promise<StockBom[]> => request('/inventory/boms'),

  inventoryCreateBom: (input: {
    name: string;
    output_item_id: number;
    output_qty: string;
    components: { component_item_id: number; qty_per_output: string }[];
  }) =>
    request<StockBom>('/inventory/boms', { method: 'POST', body: JSON.stringify(input) }),

  inventoryDeleteBom: (id: number) =>
    request<{ deleted: boolean }>(`/inventory/boms/${id}`, { method: 'DELETE' }),

  /** The effective price for (item, party, date) — null is the honest
   * "no suggestion" (the matchmaker's law: suggestions never commit). */
  inventoryEffectivePrice: (itemId: number, partyLedgerId?: number, on?: string) => {
    const params = new URLSearchParams({ item_id: String(itemId) });
    if (partyLedgerId != null) params.set('party_ledger_id', String(partyLedgerId));
    if (on) params.set('on', on);
    return request<{ rate: string | null }>(`/inventory/prices/effective?${params.toString()}`);
  },

  /** A stock pack report's JSON fold — the same rows the PDF renders. */
  stockPack: <T>(key: string, params: Record<string, string> = {}): Promise<T> => {
    const query = new URLSearchParams({ ...params, format: 'json' });
    return request(`/reports/pack/${key}?${query.toString()}`);
  },

  // ---- banking: statements, matches, the proof (ADR-026) ------------------
  /** Every bank-account ledger of the book with its balance — the banking
   *  screen's picker feed. */
  bankLedgers: async (): Promise<BankLedgerOut[]> =>
    (await request<{ ledgers: BankLedgerOut[] }>('/banking/ledgers')).ledgers,
  /** Import a statement (CSV/MT940) — validated, idempotent; dryRun is the
   *  preview (the receipt is the answer either way). */
  bankImport: (
    ledgerId: number,
    input: { format: 'csv' | 'mt940'; text: string; dry_run?: boolean; source?: string; opening_balance?: string },
  ) => request<BankImportReceipt>(`/banking/${ledgerId}/statements`, { method: 'POST', body: JSON.stringify(input) }),
  /** The statement line book with live-match state. */
  bankLines: (ledgerId: number, status: 'all' | 'unmatched' | 'matched' = 'all') =>
    request<BankLineBook>(`/banking/${ledgerId}/lines?status=${status}`),
  /** The bank ledger's own entries — the manual matchmaker's discovery
   *  surface (amounts may disagree; the proof prices the difference). */
  bankEntries: (ledgerId: number, status: 'all' | 'unmatched' | 'matched' = 'all') =>
    request<BankEntryBook>(`/banking/${ledgerId}/entries?status=${status}`),
  /** The matchmaker's ranked candidates — computed, never committed. */
  bankSuggestions: (ledgerId: number, window = 7) =>
    request<BankSuggestions>(`/banking/${ledgerId}/suggestions?window=${window}`),
  /** Record the judgement: this line is that entry (the only commit). */
  bankMatch: (
    ledgerId: number,
    target: { statement_line_id: number; entry_id: number; voucher_id: number; voucher_date: string },
  ) => request<BankMatchResult>(`/banking/${ledgerId}/matches`, { method: 'POST', body: JSON.stringify(target) }),
  /** Withdraw a match — the row stays, unmatched_at says when. */
  bankUnmatch: (statementLineId: number) =>
    request<{ statement_line_id: number; unmatched: boolean }>(
      `/banking/matches/${statementLineId}`,
      { method: 'DELETE' },
    ),
  /** Book vs bank with the proof — the anchor is the figure on the
   *  statement, or the fold when omitted. */
  bankReconciliation: (ledgerId: number, asOf: string, statementBalance?: string) => {
    const params = new URLSearchParams({ as_of: asOf });
    if (statementBalance) params.set('statement_balance', statementBalance);
    return request<BankReconciliation>(`/banking/${ledgerId}/reconciliation?${params.toString()}`);
  },
  /** The cheque register fold (the banking pack report). */
  bankCheques: (asOf: string, ledger?: string, status?: string) => {
    const params = new URLSearchParams({ as_of: asOf, format: 'json' });
    if (ledger) params.set('ledger', ledger);
    if (status) params.set('status', status);
    return request<BankChequeRegister>(`/reports/pack/banking-cheques?${params.toString()}`);
  },
};
