/**
 * Issue #2 acceptance: "a full sale can be posted mouse-free at typing
 * speed." These tests drive the real component tree with real keyboard
 * events through the keyboard-kit pipeline (jsdom), with only the network
 * mocked. Amounts are asserted as strings end-to-end (Law 3).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
  { id: 2, name: 'Local Sales', code: null, group_id: 20, group_name: 'Sales Accounts', is_system: false },
  { id: 3, name: 'CGST Payable', code: null, group_id: 21, group_name: 'Duties & Taxes', is_system: false },
  { id: 4, name: 'SGST Payable', code: null, group_id: 21, group_name: 'Duties & Taxes', is_system: false },
  { id: 5, name: 'HDFC Bank', code: null, group_id: 11, group_name: 'Bank Accounts', is_system: false },
];

const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
  { id: 9, name: 'Suspense A/c', code: null, parent_id: 1, nature: 'liability', affects_gross: false, is_root: false },
  { id: 30, name: 'Sundry Debtors', code: null, parent_id: 1, nature: 'asset', affects_gross: false, is_root: false },
  { id: 10, name: 'Cash-in-Hand', code: null, parent_id: 1, nature: 'asset', affects_gross: false, is_root: false },
  { id: 20, name: 'Sales Accounts', code: null, parent_id: 1, nature: 'income', affects_gross: true, is_root: false },
];

interface Recorded {
  method: string;
  url: string;
  body?: unknown;
}

let calls: Recorded[];
let voucherResponses: Array<{ status: number; body: unknown }>;

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  calls = [];
  voucherResponses = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/ledgers') && method === 'POST') {
      return jsonResponse(
        {
          id: 99,
          name: (body as { name: string }).name,
          code: null,
          group_id: 30,
          group_name: (body as { group_name: string }).group_name,
          is_system: false,
        },
        201,
      );
    }
    if (url.endsWith('/api/v1/vouchers') && method === 'POST') {
      const next = voucherResponses.shift();
      if (!next) throw new Error('unexpected voucher POST');
      return jsonResponse(next.body, next.status);
    }
    throw new Error(`unmocked ${method} ${url}`);
  }) as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function setup() {
  const user = userEvent.setup();
  const utils = render(<App />);
  // wait for the ledger index to be prefetched: the focused cell's picker
  // will offer 'Cash' once masters arrive
  await user.keyboard('C');
  await screen.findByRole('option', { name: /Cash/ });
  await user.keyboard('{Backspace}');
  return { user, ...utils };
}

describe('voucher screen — acceptance (issue #2)', () => {
  it('posts a full sale mouse-free at typing speed', async () => {
    voucherResponses.push({
      status: 201,
      body: {
        id: 7,
        uuid: '9af1c412-6a1f-4b0e-9b76-2c2a5b46d1aa',
        voucher_no: 'SAL-2026-27-00042',
        voucher_type: 'SAL',
        voucher_date: '2026-11-06',
        narration: null,
        reversal_of: null,
        posted_at: '2026-11-06T10:00:00Z',
        client_token: 'tok',
        replayed: false,
        entries: [],
      },
    });
    const { user } = await setup();

    // line 1: Cash debit 1180
    await user.keyboard('Cash{Enter}');
    await user.keyboard('1180{Enter}');
    await user.keyboard('{Enter}'); // skip credit (narration empty) -> next row

    // line 2: Local Sales credit 1000
    await user.keyboard('Local Sales{Enter}');
    await user.keyboard('{Enter}'); // skip debit -> credit
    await user.keyboard('1000{Enter}');

    // line 3 + 4: GST split
    await user.keyboard('CGST Payable{Enter}{Enter}90{Enter}');
    await user.keyboard('SGST Payable{Enter}{Enter}90{Enter}');

    // balanced HUD before accepting
    expect(screen.getByText('Balanced')).toBeInTheDocument();

    // accept
    await user.keyboard('{Control>}a{/Control}');
    // the banner and the screen-reader live region both announce it
    const announced = await screen.findAllByText('Voucher SAL-2026-27-00042 posted');
    expect(announced.length).toBe(2);

    // the POST: exact payload, amounts as strings (Law 3)
    const post = calls.find((c) => c.url.endsWith('/api/v1/vouchers'));
    expect(post).toBeDefined();
    const payload = post!.body as {
      voucher_type: string;
      voucher_date: string;
      client_token: string;
      entries: { ledger_id: number; dc: string; amount: string }[];
    };
    expect(payload.voucher_type).toBe('SAL');
    expect(payload.entries).toEqual([
      { ledger_id: 1, dc: 'D', amount: '1180' },
      { ledger_id: 2, dc: 'C', amount: '1000' },
      { ledger_id: 3, dc: 'C', amount: '90' },
      { ledger_id: 4, dc: 'C', amount: '90' },
    ]);
    expect(payload.entries.every((e) => typeof e.amount === 'string')).toBe(true);
    expect(payload.client_token).toMatch(/^[0-9a-f-]{36}$/);

    // the grid is ready for the next entry: one empty line, picker on it
    await waitFor(() => {
      expect(screen.getByLabelText('Line 1 particulars')).toHaveValue('');
    });
    expect(screen.getByText('Last posted: SAL-2026-27-00042')).toBeInTheDocument();
  });

  it('blocks an unbalanced voucher with the exact difference and posts nothing', async () => {
    const { user } = await setup();
    await user.keyboard('Cash{Enter}50{Enter}{Enter}');
    await user.keyboard('Local Sales{Enter}{Enter}40{Enter}');
    expect(screen.getByText('Amount left 10.00')).toBeInTheDocument();

    await user.keyboard('{Control>}a{/Control}');
    expect((await screen.findAllByText(/Debits do not equal credits/)).length).toBeGreaterThan(0);
    expect(calls.some((c) => c.url.endsWith('/api/v1/vouchers'))).toBe(false);
  });

  it('creates a missing ledger mid-flight with Alt+C and resumes entry', async () => {
    const { user } = await setup();
    await user.keyboard('Zeta Traders'); // typed, no match — create it
    await user.keyboard('{Alt>}c{/Alt}');
    expect(await screen.findByRole('dialog', { name: 'Create ledger' })).toBeInTheDocument();
    expect(screen.getByLabelText('Ledger name')).toHaveValue('Zeta Traders');

    await user.keyboard('{Enter}'); // name -> group field (Suspense A/c preselected)
    await user.keyboard('Sundry Debtors{Enter}'); // pick the group
    await user.keyboard('{Enter}'); // submit

    expect((await screen.findAllByText(/Zeta Traders.*created under Sundry Debtors/)).length).toBe(2);
    const post = calls.find((c) => c.url.endsWith('/api/v1/ledgers') && c.method === 'POST');
    expect(post?.body).toEqual({ name: 'Zeta Traders', group_name: 'Sundry Debtors' });

    // focus returned to the grid with the new ledger in place
    expect(screen.getByLabelText('Line 1 particulars')).toHaveValue('Zeta Traders');
    // and the new ledger is in the index: go back to the ledger cell (the
    // create flow advanced to debit) and trim the query
    await user.keyboard('{Shift>}{Enter}{/Shift}');
    await user.keyboard('{Backspace}{Backspace}'); // trim to 'Zeta Trade'
    expect(await screen.findByRole('option', { name: /Zeta Traders/ })).toBeInTheDocument();
  });

  it('F2 changes the date and F-keys switch the voucher type', async () => {
    const { user } = await setup();
    await user.keyboard('{F5}');
    expect(screen.getByRole('tab', { name: /Payment/ })).toHaveAttribute('aria-selected', 'true');

    await user.keyboard('{F2}');
    expect(await screen.findByRole('dialog', { name: 'Change voucher date' })).toBeInTheDocument();
    await user.keyboard('2026-04-02{Enter}');
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /2026-04-02/ })).toBeInTheDocument(),
    );

    // invalid dates are refused
    await user.keyboard('{F2}');
    await user.keyboard('2026-02-30{Enter}');
    expect(await screen.findByText('Enter a valid calendar date as YYYY-MM-DD')).toBeInTheDocument();
    await user.keyboard('{Escape}');
  });

  it('Ctrl+Enter fills the exact balancing amount', async () => {
    const { user } = await setup();
    await user.keyboard('Cash{Enter}1180{Enter}{Enter}');
    await user.keyboard('Local Sales{Enter}{Enter}'); // cursor on line 2 credit
    await user.keyboard('{Control>}{Enter}{/Control}');
    expect(screen.getByLabelText('Line 2 credit')).toHaveValue('1180');
    expect(screen.getByText('Balanced')).toBeInTheDocument();
  });

  it('Alt+N asks before discarding a dirty draft', async () => {
    const { user } = await setup();
    await user.keyboard('Cash{Enter}50{Enter}{Enter}');
    await user.keyboard('{Alt>}n{/Alt}');
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
    await user.keyboard('{Enter}'); // discard
    await waitFor(() => expect(screen.getByLabelText('Line 1 particulars')).toHaveValue(''));
  });
});
