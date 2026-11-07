/**
 * Thin fetch client for the engine API. Relative URLs: the dev server and
 * the production container both proxy /api to the engine, so the browser
 * origin is the only origin involved.
 */

import type {
  GroupOut,
  LedgerOut,
  VoucherOut,
  VoucherPostPayload,
} from './types';

const BASE = '/api/v1';

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
      headers: { 'Content-Type': 'application/json' },
      ...init,
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
    response = await fetch(`${BASE}${path}`);
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

export const api = {
  ledgers: () => request<LedgerOut[]>('/ledgers'),
  groups: () => request<GroupOut[]>('/groups'),
  createLedger: (input: CreateLedgerInput) =>
    request<LedgerOut>('/ledgers', { method: 'POST', body: JSON.stringify(input) }),
  postVoucher: (payload: VoucherPostPayload) =>
    request<VoucherOut>('/vouchers', { method: 'POST', body: JSON.stringify(payload) }),
  /** Posted vouchers, newest first (the payslip screen filters payroll runs). */
  vouchers: (limit = 200) => request<VoucherOut[]>(`/vouchers?limit=${limit}`),
  /** A voucher as its PDF (ADR-013): payslip for payroll runs with
   * `employee`, tax invoice for invoices, voucher slip otherwise. */
  voucherPdf: (voucherId: number, employee: string): Promise<Blob> =>
    requestBlob(`/vouchers/${voucherId}/pdf?employee=${encodeURIComponent(employee)}`),
};
