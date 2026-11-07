/**
 * The payslip screen (ADR-013): the posted voucher list is the run index,
 * Alt+P opens the screen, Ctrl+P prints the selected payslip PDF, Esc is
 * back to voucher entry — driven through the real keyboard-kit pipeline
 * with only the network mocked. Amounts stay strings end-to-end (Law 3).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import App from '../src/App';
import type { VoucherOut } from '../src/api/types';
import {
  employeesOfRuns,
  findRun,
  payrollRunOf,
  periodsOfEmployee,
} from '../src/payslip/model';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

function voucher(id: number, meta: Record<string, unknown> | null): VoucherOut {
  return {
    id,
    uuid: `00000000-0000-0000-0000-00000000000${id}`,
    voucher_no: `JV-2026-27-0000${id}`,
    voucher_type: 'JV',
    voucher_date: '2026-11-07',
    narration: null,
    reversal_of: null,
    posted_at: '2026-11-07T10:00:00Z',
    client_token: 'tok',
    replayed: false,
    meta,
    entries: [],
  };
}

function runDoc(employees: string[], period: string): Record<string, unknown> {
  return {
    payroll: {
      mode: 'run',
      period,
      employees: employees.map((employee) => ({
        employee,
        components: [{ name: 'Basic Salary', amount: '50000.00', kind: 'earning' }],
      })),
      computed: {
        period,
        fy: '2026',
        total_net: '50000.00',
        employees: employees.map((employee) => ({
          employee,
          gross: '50000.00',
          withheld: '0.00',
          employer: '0.00',
          net: '50000.00',
          ytd: { gross: '50000.00', withheld: '0.00', employer: '0.00', net: '50000.00', runs: 1 },
          components: [{ name: 'Basic Salary', amount: '50000.00', kind: 'earning', ytd: '50000.00' }],
        })),
      },
    },
  };
}

// newest-first, as the API returns them
const RUNS: VoucherOut[] = [
  voucher(21, runDoc(['Asha Verma', 'Rohan Iyer'], '2026-10')),
  voucher(20, runDoc(['Asha Verma'], '2026-09')),
  // not runs: a payout and a plain voucher must not become index entries
  voucher(22, { payroll: { mode: 'payout', employee: 'Asha Verma', amount: '50000.00', bank: 'HDFC Bank' } }),
  voucher(23, null),
];

interface Recorded {
  method: string;
  url: string;
}

let calls: Recorded[];
let openSpy: MockInstance<typeof window.open>;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

function blobResponse(): Response {
  return {
    ok: true,
    status: 200,
    blob: async () => new Blob(['%PDF-1.4 (mock)'], { type: 'application/pdf' }),
  } as unknown as Response;
}

beforeEach(() => {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url });
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/vouchers?limit=200')) return jsonResponse(RUNS);
    if (url.includes('/api/v1/vouchers/') && url.includes('/pdf')) return blobResponse();
    throw new Error(`unmocked ${method} ${url}`);
  }) as typeof fetch;

  // jsdom ships neither; the print flow needs both
  window.URL.createObjectURL = vi.fn(() => 'blob:mock-payslip');
  window.URL.revokeObjectURL = vi.fn();
  openSpy = vi.spyOn(window, 'open').mockReturnValue({} as Window);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // NOTE: the URL mock methods stay installed — without vitest globals,
  // RTL's cleanup runs at the *next* render, so this screen unmounts after
  // this hook; the next beforeEach reinstalls fresh mocks anyway.
});

async function setup() {
  const user = userEvent.setup();
  const utils = render(<App />);
  // the voucher screen is up once masters arrive
  await screen.findByLabelText('Line 1 particulars');
  return { user, ...utils };
}

async function openPayslips(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}p{/Alt}');
  await screen.findByLabelText('Employee');
}

describe('payslip model — the run index', () => {
  it('treats only payroll runs as index entries', () => {
    const runs = RUNS.filter((v) => payrollRunOf(v) !== null);
    expect(runs.map((r) => r.id)).toEqual([21, 20]);
    expect(employeesOfRuns(RUNS)).toEqual(['Asha Verma', 'Rohan Iyer']);
    expect(periodsOfEmployee(RUNS, 'Asha Verma')).toEqual(['2026-10', '2026-09']);
    expect(periodsOfEmployee(RUNS, 'Rohan Iyer')).toEqual(['2026-10']);
    expect(findRun(RUNS, 'Asha Verma', '2026-09')?.id).toBe(20);
    expect(findRun(RUNS, 'Rohan Iyer', '2026-09')).toBeNull();
  });
});

describe('payslip screen — acceptance (ADR-013)', () => {
  it('Alt+P opens the screen with the run index; Esc is back', async () => {
    const { user } = await setup();
    await openPayslips(user);

    // fetched the voucher list once, newest-first
    expect(calls.some((c) => c.url.endsWith('/api/v1/vouchers?limit=200'))).toBe(true);

    // the employee picker lists run employees only (payouts are not runs)
    const employeeSelect = screen.getByLabelText('Employee') as HTMLSelectElement;
    expect([...employeeSelect.options].map((o) => o.value)).toEqual(['Asha Verma', 'Rohan Iyer']);
    expect(employeeSelect.value).toBe('Asha Verma'); // first seen, selected

    // periods for Asha, newest first
    const periodSelect = screen.getByLabelText('Payroll period') as HTMLSelectElement;
    expect([...periodSelect.options].map((o) => o.value)).toEqual(['2026-10', '2026-09']);

    // the preview shows the computed figures as strings (Law 3) — display
    // only, never client arithmetic
    expect(screen.getByText('Net pay')).toBeInTheDocument();
    expect(screen.getAllByText('50000.00').length).toBeGreaterThan(1);

    // Esc is the universal back: the voucher screen returns
    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
  });

  it('Ctrl+P fetches the payslip PDF for the selected employee & period', async () => {
    const { user } = await setup();
    await openPayslips(user);

    await user.keyboard('{Control>}p{/Control}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());

    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/21/pdf?employee=Asha%20Verma');
    expect(window.URL.createObjectURL).toHaveBeenCalled();
    expect(openSpy).toHaveBeenCalledWith('blob:mock-payslip', '_blank', 'noopener');
  });

  it('switching employee switches periods and the printed voucher', async () => {
    const { user } = await setup();
    await openPayslips(user);

    await user.selectOptions(screen.getByLabelText('Employee'), 'Rohan Iyer');
    const periodSelect = screen.getByLabelText('Payroll period') as HTMLSelectElement;
    expect([...periodSelect.options].map((o) => o.value)).toEqual(['2026-10']);

    await user.keyboard('{Control>}p{/Control}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/21/pdf?employee=Rohan%20Iyer');
  });

  it('an older period prints that period’s voucher', async () => {
    const { user } = await setup();
    await openPayslips(user);

    await user.selectOptions(screen.getByLabelText('Payroll period'), '2026-09');
    await user.keyboard('{Control>}p{/Control}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/20/pdf?employee=Asha%20Verma');
  });

  it('a blocked PDF tab surfaces a loud error, not silence', async () => {
    openSpy.mockReset();
    openSpy.mockReturnValue(null);
    const { user } = await setup();
    await openPayslips(user);

    await user.keyboard('{Control>}p{/Control}');
    expect(await screen.findByRole('alert')).toHaveTextContent(/blocked the PDF tab/);
  });
});
