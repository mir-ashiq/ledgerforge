/**
 * The daybook screen (ADR-015): every posted voucher browsable and
 * reprintable — Alt+B opens, ↑↓/j/k walk the list, the selection previews
 * on the right, Enter/Ctrl+P reprint through the same endpoint the payslip
 * and voucher screens use, Esc is back to entry. Driven through the real
 * keyboard-kit pipeline with only the network mocked; amounts stay strings
 * end-to-end (Law 3).
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import App from '../src/App';
import type { VoucherOut } from '../src/api/types';
import { docKindOf, matchesText, money, printEmployeeArg } from '../src/daybook/model';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

function voucher(
  id: number,
  over: Partial<VoucherOut> & Pick<VoucherOut, 'voucher_no' | 'voucher_type' | 'amount'>,
): VoucherOut {
  return {
    id,
    uuid: `00000000-0000-0000-0000-00000000000${id}`,
    voucher_date: '2026-11-07',
    narration: null,
    reversal_of: null,
    posted_at: '2026-11-07T10:00:00Z',
    client_token: 'tok',
    replayed: false,
    meta: null,
    entries: [],
    ...over,
  };
}

function runDoc(employees: string[]): Record<string, unknown> {
  return {
    payroll: {
      mode: 'run',
      period: '2026-11',
      employees: employees.map((employee) => ({
        employee,
        components: [{ name: 'Basic Salary', amount: '50000.00', kind: 'earning' }],
      })),
    },
  };
}

// newest first, as the API returns them
const VOUCHERS: VoucherOut[] = [
  voucher(31, {
    voucher_no: 'SAL-2026-27-00007',
    voucher_type: 'SAL',
    amount: '1180.00000000', // storage scale: displays as 1180.00
    narration: 'counter sale',
    entries: [
      { line_no: 1, ledger_id: 1, ledger_name: 'Cash', dc: 'D', amount: '1180.00000000' },
      { line_no: 2, ledger_id: 2, ledger_name: 'Local Sales', dc: 'C', amount: '1000.00000000' },
    ],
  }),
  voucher(30, {
    voucher_no: 'JV-2026-27-00006',
    voucher_type: 'JV',
    amount: '100000.00',
    narration: 'payroll — 2026-11',
    meta: runDoc(['Asha Verma', 'Rohan Iyer']),
  }),
  voucher(29, {
    voucher_no: 'PYT-2026-27-00005',
    voucher_type: 'PYT',
    amount: '13687.50',
    meta: { payroll: { mode: 'payout', employee: 'Asha Verma', amount: '13687.50', bank: 'HDFC Bank' } },
  }),
  voucher(28, {
    voucher_no: 'JV-2026-27-00004',
    voucher_type: 'JV',
    amount: '9000.00',
    meta: runDoc(['Meera Nair']),
  }),
  voucher(27, {
    voucher_no: 'SAL-2026-27-00003R',
    voucher_type: 'SAL',
    amount: '590.00',
    reversal_of: 26,
  }),
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
    if (url.startsWith('/api/v1/vouchers?')) return jsonResponse(VOUCHERS);
    if (url.includes('/api/v1/vouchers/') && url.includes('/pdf')) return blobResponse();
    throw new Error(`unmocked ${method} ${url}`);
  }) as typeof fetch;

  // jsdom ships neither; the print flow needs both
  window.URL.createObjectURL = vi.fn(() => 'blob:mock-daybook');
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

async function openDaybook(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}b{/Alt}');
  await screen.findByRole('grid', { name: 'Posted vouchers' });
}

/** The voucher currently selected (aria-selected row, in list order). */
function selectedRow(): HTMLElement {
  const row = document.querySelector('.daybook-row[aria-selected="true"]');
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

describe('daybook model — display money & document kinds', () => {
  it('money() shows storage scale as cents, keeps sub-cent exactly (Law 3)', () => {
    expect(money('1180.00000000')).toBe('1180.00');
    expect(money('100.00')).toBe('100.00');
    expect(money('100')).toBe('100');
    expect(money('100.005')).toBe('100.005'); // real sub-cent precision: as posted
    expect(money(undefined)).toBe('—');
    expect(money(null)).toBe('—');
  });

  it('docKindOf labels the business document a voucher carried', () => {
    expect(docKindOf(VOUCHERS[1]!)).toBe('payroll-run');
    expect(docKindOf(VOUCHERS[2]!)).toBe('payroll-payout');
    expect(docKindOf(VOUCHERS[0]!)).toBeNull();
  });

  it('printEmployeeArg: single-employee runs print directly, multi need the payslip screen', () => {
    expect(printEmployeeArg(VOUCHERS[1]!)).toBe('many');
    expect(printEmployeeArg(VOUCHERS[3]!)).toBe('Meera Nair');
    expect(printEmployeeArg(VOUCHERS[0]!)).toBeNull(); // not a run: prints as itself
    expect(printEmployeeArg(VOUCHERS[2]!)).toBeNull(); // payouts print as slips
  });

  it('matchesText searches no, type, narration and ledgers, case-insensitively', () => {
    expect(matchesText(VOUCHERS[0]!, 'counter')).toBe(true);
    expect(matchesText(VOUCHERS[0]!, 'CASH')).toBe(true);
    expect(matchesText(VOUCHERS[0]!, 'SAL-2026-27-00007')).toBe(true);
    expect(matchesText(VOUCHERS[0]!, 'hdfc')).toBe(false);
    expect(matchesText(VOUCHERS[0]!, '  ')).toBe(true);
  });
});

describe('daybook screen — acceptance (ADR-015)', () => {
  it('Alt+B opens the daybook with every posted voucher; Esc is back', async () => {
    const { user } = await setup();
    await openDaybook(user);

    // the engine list, once, with the browse filters (all empty = bare list)
    expect(calls.some((c) => c.url.endsWith('/api/v1/vouchers?limit=200'))).toBe(true);

    // five rows, newest first: the first row is selected
    const rows = within(screen.getByRole('grid', { name: 'Posted vouchers' })).getAllByRole('row');
    expect(rows.length).toBe(6); // header + 5 vouchers
    expect(selectedRow().textContent).toContain('SAL-2026-27-00007');
    expect(screen.getByText('5 vouchers')).toBeInTheDocument();

    // the amount column displays the engine-computed string as cents
    expect(screen.getAllByText('1180.00').length).toBeGreaterThan(0);

    // chips mark what each voucher carried (two runs → two payroll chips)
    expect(screen.getAllByText('payroll').length).toBe(2);
    expect(screen.getByText('payout')).toBeInTheDocument();
    expect(screen.getByText('reversal')).toBeInTheDocument();

    // Esc is the universal back: the voucher screen returns
    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
  });

  it('↑↓ and j/k move the selection; the detail pane follows', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.keyboard('{ArrowDown}');
    expect(selectedRow().textContent).toContain('JV-2026-27-00006');
    // the detail pane previews the selection's entries
    expect(screen.getByRole('table', { name: 'Voucher entries' })).toBeInTheDocument();

    await user.keyboard('j'); // vim down
    expect(selectedRow().textContent).toContain('PYT-2026-27-00005');

    await user.keyboard('k'); // vim up
    expect(selectedRow().textContent).toContain('JV-2026-27-00006');

    await user.keyboard('{ArrowUp}');
    expect(selectedRow().textContent).toContain('SAL-2026-27-00007');
  });

  it('Enter reprints the selected voucher through the same endpoint (ADR-013)', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.keyboard('{Enter}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/31/pdf');
    expect(openSpy).toHaveBeenCalledWith('blob:mock-daybook', '_blank', 'noopener');
  });

  it('Ctrl+P reprints any historical voucher — a payout prints as its slip', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.keyboard('{ArrowDown}{ArrowDown}');
    expect(selectedRow().textContent).toContain('PYT-2026-27-00005');
    await user.keyboard('{Control>}p{/Control}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/29/pdf');
  });

  it('a one-employee payroll run prints with ?employee=', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}');
    expect(selectedRow().textContent).toContain('JV-2026-27-00004');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(openSpy).toHaveBeenCalled());
    const pdfCall = calls.find((c) => c.url.includes('/pdf'));
    expect(pdfCall?.url).toBe('/api/v1/vouchers/28/pdf?employee=Meera%20Nair');
  });

  it('a multi-employee run redirects to the payslip screen, fetching nothing', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.keyboard('{ArrowDown}');
    expect(selectedRow().textContent).toContain('JV-2026-27-00006');
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent(/Payslips screen \(Alt\+P\)/);
    expect(calls.some((c) => c.url.includes('/pdf'))).toBe(false);
  });

  it('the find box filters the list client-side', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.type(screen.getByLabelText('Find vouchers'), 'counter');
    expect(await screen.findByText('1 voucher')).toBeInTheDocument();
    expect(screen.queryByText('payroll')).not.toBeInTheDocument(); // narrowed out
  });

  it('the type and date filters re-query the engine', async () => {
    const { user } = await setup();
    await openDaybook(user);

    await user.selectOptions(screen.getByLabelText('Voucher type'), 'JV');
    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith('/api/v1/vouchers?type=JV&limit=200'))).toBe(true),
    );

    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-11-01' } });
    await waitFor(() =>
      expect(
        calls.some((c) =>
          c.url.endsWith('/api/v1/vouchers?type=JV&date_from=2026-11-01&limit=200'),
        ),
      ).toBe(true),
    );
  });
});
