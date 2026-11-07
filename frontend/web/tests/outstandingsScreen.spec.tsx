/**
 * The outstandings screen (Phase 2, ADR-023): Alt+I opens the outstanding
 * book — bills receivable with ageing buckets and the on-account pools; R/P
 * switch sides (bills-payable re-fetches), F focuses the party filter, the
 * ageing footer states the bucket totals the engine computed. Driven
 * through the real keyboard pipeline with only the network mocked; every
 * amount is asserted as the engine's string (Law 3).
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { OutstandingReport } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];
const BOOKS = [
  { id: 1, name: 'My Company', slug: 'default', base_currency: 'INR', fy_start_month: 4, is_archived: false, created_at: '2026-04-01T10:00:00Z' },
];

function receivable(): OutstandingReport {
  return {
    side: 'dr',
    as_of: '2026-11-07',
    party: null,
    bills: [
      {
        party: 'Acme Traders',
        ref: 'INV-42',
        side: 'dr',
        opened_on: '2026-08-01',
        due_on: '2026-08-31',
        outstanding: '1180.00',
        bucket: '61-90',
        interest_annual: '18',
      },
      {
        party: 'Beacon Retail',
        ref: 'INV-7',
        side: 'dr',
        opened_on: '2026-10-01',
        due_on: '2026-10-31',
        outstanding: '500.00',
        bucket: '0-30',
        interest_annual: null,
      },
    ],
    on_account: { 'Acme Traders': '50.00' },
    party_totals: { 'Acme Traders': '1230.00', 'Beacon Retail': '500.00' },
    ageing: { 'not due': '0.00', '0-30': '500.00', '31-60': '0.00', '61-90': '1180.00', '90+': '0.00' },
    total: '1730.00',
  };
}

function payable(): OutstandingReport {
  return {
    side: 'cr',
    as_of: '2026-11-07',
    party: null,
    bills: [
      {
        party: 'Zen Supplies',
        ref: 'BILL-1',
        side: 'cr',
        opened_on: '2026-10-15',
        due_on: '2026-10-30',
        outstanding: '-2000.00',
        bucket: '0-30',
        interest_annual: null,
      },
    ],
    on_account: {},
    party_totals: { 'Zen Supplies': '-2000.00' },
    ageing: { 'not due': '0.00', '0-30': '-2000.00', '31-60': '0.00', '61-90': '0.00', '90+': '0.00' },
    total: '-2000.00',
  };
}

let fetches: string[];

beforeEach(() => {
  fetches = [];
  window.localStorage.clear();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    fetches.push(url);
    if (url.endsWith('/api/v1/ledgers')) return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/books')) return jsonResponse(BOOKS);
    if (url.includes('/reports/pack/bills-receivable')) return jsonResponse(receivable());
    if (url.includes('/reports/pack/bills-payable')) return jsonResponse(payable());
    throw new Error(`unmocked ${url}`);
  }) as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

async function setup() {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByLabelText('Line 1 particulars');
  return user;
}

describe('outstandings screen (ADR-023, Phase 2)', () => {
  it('Alt+I opens the receivable book with ageing and on-account rows', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}i{/Alt}');

    const table = await screen.findByLabelText('Bills receivable with ageing');
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(4); // header + 2 bills + 1 on-account row
    expect(within(table).getByText('INV-42')).toBeTruthy();
    expect(within(table).getByText('1180.00')).toBeTruthy();
    expect(within(table).getByText('61-90')).toBeTruthy();
    expect(within(table).getByText('on account')).toBeTruthy();

    // the ageing footer states the engine's bucket totals and the grand total
    expect(screen.getByText('0-30:')).toBeTruthy();
    const totalChip = screen.getByText('Total:').closest('span')!;
    expect(within(totalChip as HTMLElement).getByText('1730.00')).toBeTruthy();
  });

  it('P switches to the payable side and re-fetches; R is back', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}i{/Alt}');
    await screen.findByText('INV-42');

    await user.keyboard('p');
    await waitFor(() => expect(screen.getByText('BILL-1')).toBeTruthy());
    const payTable = screen.getByLabelText('Bills payable with ageing');
    expect(within(payTable).getByText('-2000.00')).toBeTruthy();
    expect(fetches.some((u) => u.includes('bills-payable'))).toBe(true);

    await user.keyboard('r');
    await waitFor(() => expect(screen.getByText('INV-42')).toBeTruthy());
  });

  it('F filters by party, client-side over the loaded rows', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}i{/Alt}');
    await screen.findByText('INV-42');

    await user.keyboard('f');
    await user.keyboard('beacon');
    expect(screen.queryByText('INV-42')).toBeNull();
    expect(screen.getByText('INV-7')).toBeTruthy();
    // the on-account row of another party is filtered out too
    expect(screen.queryByText('on account')).toBeNull();
  });

  it('Esc is back to the voucher screen', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}i{/Alt}');
    await screen.findByText('INV-42');
    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
  });
});
