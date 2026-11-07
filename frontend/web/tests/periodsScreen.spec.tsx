/**
 * The periods screen (Phase 1, ADR-008 in the UI): Alt+E opens it; the
 * periods list with status chips; L locks (freeze for review), U unlocks,
 * P previews exactly what closing would post (the zeroing sweep lines and
 * the net profit), and Enter confirms the close from the preview — the
 * sweep posts as an FYC voucher, the period is closed, final. N creates a
 * period. Driven through the real keyboard-kit pipeline with only the
 * network mocked.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { FyCloseReportOut, PeriodCloseResult, PeriodOut } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

function period(id: number, name: string, over: Partial<PeriodOut> = {}): PeriodOut {
  return {
    id,
    name,
    starts_on: '2025-04-01',
    ends_on: '2026-03-31',
    status: 'open',
    locked_at: null,
    closed_at: null,
    closed_by: null,
    ...over,
  };
}

let PERIODS: PeriodOut[];

const CLOSE_PREVIEW: FyCloseReportOut = {
  as_of: '2026-03-31',
  lines: [
    { bucket: 'income', ledger_id: 20, ledger_name: 'Local Sales', dc: 'D', amount: '125000.00' },
    { bucket: 'expense', ledger_id: 30, ledger_name: 'Rent', dc: 'C', amount: '60000.00' },
    { bucket: 'profit_loss', ledger_id: 40, ledger_name: 'Profit & Loss A/c', dc: 'C', amount: '65000.00' },
  ],
  net_profit: '65000.00',
  totals: { income: '125000.00', expense: '60000.00' },
};

const CLOSE_RESULT: PeriodCloseResult = {
  period: period(1, 'FY 2025-26', {
    status: 'closed',
    closed_by: 'admin',
    closed_at: '2026-11-07T12:00:00Z',
  }),
  sweep_voucher: {
    id: 99,
    uuid: '00000000-0000-0000-0000-000000000099',
    voucher_no: 'FYC-2026-27-00001',
    voucher_type: 'FYC',
    voucher_date: '2026-04-01',
    amount: '125000.00',
    narration: 'FY close sweep',
    reversal_of: null,
    posted_at: '2026-11-07T12:00:00Z',
    client_token: 'sweep',
    replayed: false,
    meta: null,
    entries: [],
  },
  sweep_posted: true,
};

interface Recorded {
  method: string;
  url: string;
}

let calls: Recorded[];

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => {
  calls = [];
  PERIODS = [
    period(1, 'FY 2025-26'),
    period(2, 'FY 2026-27', { starts_on: '2026-04-01', ends_on: '2027-03-31' }),
  ];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url });
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/books')) return jsonResponse([]);
    if (url.endsWith('/api/v1/periods') && method === 'POST') {
      const payload = JSON.parse(String(init?.body)) as PeriodOut;
      const created = period(PERIODS.length + 1, payload.name, {
        starts_on: payload.starts_on,
        ends_on: payload.ends_on,
      });
      PERIODS.push(created);
      return jsonResponse(created, 201);
    }
    if (url.endsWith('/lock')) {
      const id = Number(url.split('/periods/')[1]?.split('/')[0]);
      PERIODS = PERIODS.map((p) =>
        p.id === id ? { ...p, status: 'locked', locked_at: '2026-11-07T11:00:00Z' } : p,
      );
      return jsonResponse(PERIODS.find((p) => p.id === id));
    }
    if (url.endsWith('/unlock')) {
      const id = Number(url.split('/periods/')[1]?.split('/')[0]);
      PERIODS = PERIODS.map((p) => (p.id === id ? { ...p, status: 'open', locked_at: null } : p));
      return jsonResponse(PERIODS.find((p) => p.id === id));
    }
    if (url.endsWith('/close-preview')) return jsonResponse(CLOSE_PREVIEW);
    if (url.endsWith('/close')) {
      PERIODS = PERIODS.map((p) =>
        p.id === 1
          ? { ...p, status: 'closed', closed_by: 'admin', closed_at: '2026-11-07T12:00:00Z' }
          : p,
      );
      return jsonResponse(CLOSE_RESULT);
    }
    if (url.endsWith('/api/v1/periods')) return jsonResponse(PERIODS);
    throw new Error(`unmocked ${method} ${url}`);
  }) as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function setup() {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByLabelText('Line 1 particulars');
  return user;
}

async function openPeriods(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}e{/Alt}');
  await screen.findByRole('grid', { name: 'Periods' });
}

describe('the periods screen (Phase 1, ADR-008)', () => {
  it('Alt+E opens the screen with the periods and their statuses; Esc is back', async () => {
    const user = await setup();
    await openPeriods(user);

    const grid = screen.getByRole('grid', { name: 'Periods' });
    expect(grid).toHaveTextContent('FY 2025-26');
    expect(grid).toHaveTextContent('FY 2026-27');
    expect(grid).toHaveTextContent('2025-04-01');
    expect(screen.getAllByText('open').length).toBe(2);
    expect(screen.getByText('2 periods')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
  });

  it('L locks the highlighted period; U unlocks it', async () => {
    const user = await setup();
    await openPeriods(user);

    await user.keyboard('l');
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/periods/1/lock'))).toBe(true),
    );
    await waitFor(() => expect(screen.getByText('locked')).toBeInTheDocument());

    await user.keyboard('u');
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/periods/1/unlock'))).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText('locked')).toBeNull());
  });

  it('P previews exactly what closing posts; Enter confirms — the sweep posts, final', async () => {
    const user = await setup();
    await openPeriods(user);

    await user.keyboard('p');
    const dialog = await screen.findByRole('dialog', { name: /Close FY 2025-26/ });
    // every zeroing line, exactly as the engine would post it
    const sweep = within(dialog).getByRole('table', { name: 'The close sweep' });
    expect(sweep).toHaveTextContent('Local Sales');
    expect(sweep).toHaveTextContent('Rent');
    expect(sweep).toHaveTextContent('Profit & Loss A/c');
    expect(dialog).toHaveTextContent('Net profit: 65000.00');

    await user.keyboard('{Enter}');
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/periods/1/close'))).toBe(true),
    );
    // the outcome is announced with the sweep voucher's number
    expect(await screen.findByText(/sweep posted as FYC-2026-27-00001/)).toBeInTheDocument();
    // the period shows closed in the list
    await waitFor(() => expect(screen.getByText('closed')).toBeInTheDocument());
  });

  it('Esc in the preview keeps the period open — nothing posts', async () => {
    const user = await setup();
    await openPeriods(user);

    await user.keyboard('p');
    await screen.findByRole('dialog', { name: /Close FY 2025-26/ });
    await user.keyboard('{Escape}');

    expect(screen.queryByRole('dialog', { name: /Close FY 2025-26/ })).toBeNull();
    expect(calls.some((c) => c.url.endsWith('/close'))).toBe(false);
    // still on the periods screen
    expect(screen.getByRole('grid', { name: 'Periods' })).toBeInTheDocument();
  });

  it('N creates a period through the dialog', async () => {
    const user = await setup();
    await openPeriods(user);

    await user.keyboard('n');
    const dialog = await screen.findByRole('dialog', { name: 'Create a period' });
    await user.type(within(dialog).getByLabelText('Period name'), 'FY 2027-28');
    fireEventDateChange(within(dialog).getByLabelText('Period start (YYYY-MM-DD)'), '2027-04-01');
    fireEventDateChange(within(dialog).getByLabelText('Period end (YYYY-MM-DD)'), '2028-03-31');
    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/api/v1/periods'))).toBe(true),
    );
    await waitFor(() => expect(screen.getByText('FY 2027-28')).toBeInTheDocument());
  });

  it('the screen is palette-visible like every command', async () => {
    const user = await setup();
    await user.keyboard('{Control>}k{/Control}');
    const palette = await screen.findByRole('dialog');
    expect(palette).toHaveTextContent(/Periods — lock & the financial-year close/i);
  });
});

/** jsdom date inputs need a value change through fireEvent for type="date". */
function fireEventDateChange(element: HTMLElement, value: string) {
  const proto = Object.getPrototypeOf(element) as Record<string, unknown>;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') as PropertyDescriptor;
  descriptor.set?.call(element, value);
  element.dispatchEvent(new Event('change', { bubbles: true }));
}
