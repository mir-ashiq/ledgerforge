/**
 * Thin fetch client for the engine API. Relative URLs: the dev server and
 * the production container both proxy /api to the engine, so the browser
 * origin is the only origin involved.
 */

import type {
  BookCreateInput,
  BookOut,
  BookSeededOut,
  BookUpdateInput,
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
  /** The outstanding book with ageing — receivable ('dr') or payable ('cr'),
   * optionally one party (ADR-023). */
  outstanding: (side: 'dr' | 'cr', party?: string) => {
    const params = new URLSearchParams({ format: 'json' });
    if (party) params.set('party', party);
    const key = side === 'dr' ? 'bills-receivable' : 'bills-payable';
    return request<OutstandingReport>(`/reports/pack/${key}?${params.toString()}`);
  },
};
