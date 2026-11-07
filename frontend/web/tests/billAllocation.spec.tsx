/**
 * The bill-allocation dialog (Phase 2, ADR-023): Alt+L with the cursor on a
 * party line — the on-account / against-ref decision at entry time. The
 * receipt flow TallyPrime users live in: settle open bills, the remainder
 * lands on account (or opens a new ref), Ctrl+A posts the voucher with the
 * allocations as meta.bills, and the engine's 422 keeps the draft intact.
 * Driven through the real keyboard pipeline with only the network mocked;
 * amounts are asserted as strings end-to-end (Law 3).
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
  { id: 5, name: 'HDFC Bank', code: null, group_id: 11, group_name: 'Bank Accounts', is_system: false },
  { id: 30, name: 'Acme Traders', code: null, group_id: 30, group_name: 'Sundry Debtors', is_system: false },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
  { id: 30, name: 'Sundry Debtors', code: null, parent_id: 1, nature: 'asset', affects_gross: false, is_root: false },
];

const OPEN_BILLS = {
  party: 'Acme Traders',
  as_of: '2026-11-07',
  bills: [
    {
      ref: 'INV-42',
      side: 'dr',
      opened_on: '2026-08-01',
      due_on: '2026-08-31',
      outstanding: '1000.00',
      bucket: '61-90',
      interest_annual: '18',
    },
    {
      ref: 'ADV-1',
      side: 'cr',
      opened_on: '2026-10-01',
      due_on: '2026-10-01',
      outstanding: '-200.00',
      bucket: 'not due',
      interest_annual: null,
    },
  ],
  on_account: '0.00',
};

interface Recorded {
  method: string;
  url: string;
  body?: unknown;
}

let calls: Recorded[];
let postedBodies: Array<Record<string, unknown>>;

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => {
  calls = [];
  postedBodies = [];
  window.localStorage.clear();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.includes('/reports/pack/bills-open')) return jsonResponse(OPEN_BILLS);
    if (url.endsWith('/api/v1/vouchers') && method === 'POST') {
      postedBodies.push(body as Record<string, unknown>);
      return jsonResponse(
        {
          id: 7,
          uuid: 'u-7',
          voucher_no: 'RCT-2026-27-00001',
          voucher_type: 'RCT',
          voucher_date: '2026-11-07',
          amount: '750.00',
          narration: null,
          reversal_of: null,
          posted_at: '2026-11-07T10:00:00Z',
          client_token: 'tok',
          replayed: false,
          entries: [],
        },
        201,
      );
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
  render(<App />);
  await screen.findByLabelText('Line 1 particulars');
  return user;
}

/** A receipt line 1 — the party credited 750 — resting the cursor on the
 * party's credit cell (no trailing Enter: the cursor stays on the party
 * line), so Alt+L sees the active party and its movement. */
async function typePartyCreditLine(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{F6}'); // Receipt
  await user.keyboard('Acme Traders{Enter}'); // picker accepts, cursor -> debit
  await user.keyboard('{Enter}'); // skip the empty debit -> credit
  await user.keyboard('750'); // credit the party; the cursor rests here
}

/** Line 2 — the bank debited 750 — from wherever the dialog left focus. */
async function typeBankDebitLine(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Enter}'); // advance past the party line to line 2's ledger
  await user.keyboard('HDFC Bank{Enter}');
  await user.keyboard('750{Enter}');
}

describe('bill allocation dialog (ADR-023, Phase 2)', () => {
  it('Alt+L settles open bills with the remainder on account, and the post carries meta.bills', async () => {
    const user = await setup();
    await typePartyCreditLine(user);

    await user.keyboard('{Alt>}l{/Alt}');
    const dialog = await screen.findByLabelText('Allocate to bills');
    // the settleable receivable bill is offered with its facts; the advance rides along, dimmed
    expect(within(dialog).getByText('INV-42')).toBeTruthy();
    expect(within(dialog).getByText('1000.00')).toBeTruthy();
    expect(within(dialog).getByLabelText('Allocate to INV-42')).toBeTruthy();
    expect(within(dialog).getAllByText('ADV-1').length).toBeGreaterThan(0);

    await user.click(within(dialog).getByLabelText('Allocate to INV-42'));
    await user.keyboard('700');
    // 700 of 750 allocated → remainder 50, on account by default
    expect(within(dialog).getByText(/Remainder/).textContent).toContain('50');

    await user.keyboard('{Enter}');
    // the armed allocation shows in the header chip
    await waitFor(() =>
      expect(screen.getByText(/Bills: Acme Traders \(2 refs\)/)).toBeTruthy(),
    );

    // complete the voucher: line 2 debits the bank, then accept
    await typeBankDebitLine(user);
    await user.keyboard('{Control>}a{/Control}');

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    const payload = postedBodies[0]!;
    expect(payload['meta']).toEqual({
      bills: {
        party: 'Acme Traders',
        refs: [
          { kind: 'against', ref: 'INV-42', cr: '700' },
          { kind: 'on_account', cr: '50' },
        ],
      },
    });
    // the next draft starts clean — no stale allocation
    await waitFor(() => expect(screen.queryByText(/Bills: Acme Traders/)).toBeNull());
  });

  it('refuses to open without a party movement; a non-party line explains itself', async () => {
    const user = await setup();
    // Cash on line 1 — a ledger, but not a party (not Sundry Debtors/Creditors)
    await user.keyboard('C');
    await screen.findByRole('option', { name: /Cash/ });
    await user.keyboard('{Enter}');
    await user.keyboard('{Alt>}l{/Alt}');
    // the banner and the live region both carry the explanation
    const notes = await screen.findAllByText(/not a party ledger/i);
    expect(notes.length).toBeGreaterThan(0);
  });

  it('the remainder can open a new ref instead of on-account', async () => {
    const user = await setup();
    await typePartyCreditLine(user);
    await user.keyboard('{Alt>}l{/Alt}');
    const dialog = await screen.findByLabelText('Allocate to bills');

    // nothing allocated: the whole 750 opens a new bill
    await user.click(within(dialog).getByRole('radio', { name: /open new ref/i }));
    await user.click(within(dialog).getByLabelText('New bill ref'));
    await user.keyboard('ADV-2');
    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(screen.getByText(/Bills: Acme Traders \(1 ref\)/)).toBeTruthy(),
    );
    await typeBankDebitLine(user);
    await user.keyboard('{Control>}a{/Control}');

    await waitFor(() => expect(postedBodies).toHaveLength(1));
    expect(postedBodies[0]!['meta']).toEqual({
      bills: {
        party: 'Acme Traders',
        refs: [{ kind: 'new', ref: 'ADV-2', cr: '750', due_days: 30 }],
      },
    });
  });

  it('over-allocating the movement is caught client-side; the engine stays the authority', async () => {
    const user = await setup();
    await typePartyCreditLine(user);
    await user.keyboard('{Alt>}l{/Alt}');
    const dialog = await screen.findByLabelText('Allocate to bills');

    await user.click(within(dialog).getByLabelText('Allocate to INV-42'));
    await user.keyboard('900'); // under the bill's 1000, over the voucher's 750
    await user.keyboard('{Enter}');
    // the dialog refuses: allocations exceed the voucher's party movement
    expect(within(dialog).getByText(/exceed/i)).toBeTruthy();
    // nothing was applied — no armed chip
    expect(screen.queryByText(/Bills: Acme Traders/)).toBeNull();
  });

  it('the party feed is engine-backed: bills-open with format=json (plugin API 0.8)', async () => {
    const user = await setup();
    await typePartyCreditLine(user);
    await user.keyboard('{Alt>}l{/Alt}');
    await screen.findByLabelText('Allocate to bills');
    const feed = calls.find((c) => c.url.includes('/reports/pack/bills-open'));
    expect(feed).toBeTruthy();
    expect(feed!.url).toContain('party=Acme%20Traders');
    expect(feed!.url).toContain('format=json');
  });
});
