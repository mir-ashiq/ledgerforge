/**
 * The banking screen (ADR-026, Phase 5): Alt+B opens it, the bank picker
 * loads from GET /banking/ledgers, the line book renders with match
 * state, the matchmaker's suggestions never commit themselves (M posts
 * the one explicit match), U withdraws a match, and the reconciliation
 * proof renders the engine's exact verdict — driven through the real
 * keyboard-kit pipeline with only the network mocked.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type {
  BankLedgerOut,
  BankLineBook,
  BankReconciliation,
  BankSuggestions,
} from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

const BANKS: BankLedgerOut[] = [{ id: 7, name: 'HDFC Current', balance: '15000.00' }];

const LINE_BOOK: BankLineBook = {
  ledger_id: 7,
  ledger_name: 'HDFC Current',
  status: 'all',
  lines: [
    {
      id: 101,
      batch_id: 1,
      value_date: '2026-04-03',
      dc: 'D',
      amount: '2500.00',
      reference: 'UPI123456',
      narrative: 'UPI/Payment to Ramesh',
      cheque_no: null,
      match: null,
    },
    {
      id: 102,
      batch_id: 1,
      value_date: '2026-04-05',
      dc: 'C',
      amount: '15000.00',
      reference: 'NEFT987',
      narrative: 'NEFT Credit from Acme',
      cheque_no: null,
      match: {
        voucher_id: 11,
        voucher_no: 'RCT-2026-27-00002',
        voucher_date: '2026-04-05',
        entry_id: 21,
        matched_at: '2026-11-08T09:00:00+00:00',
        matched_by: 'anonymous',
        unmatched_at: null,
        live: true,
      },
    },
  ],
};

const SUGGESTIONS: BankSuggestions = {
  ledger_id: 7,
  window_days: 7,
  lines: [
    {
      statement_line_id: 101,
      value_date: '2026-04-03',
      dc: 'D',
      amount: '2500.00',
      reference: 'UPI123456',
      narrative: 'UPI/Payment to Ramesh',
      cheque_no: null,
      suggestions: [
        {
          entry_id: 31,
          voucher_id: 12,
          voucher_no: 'PYT-2026-27-00001',
          voucher_date: '2026-04-03',
          dc: 'D',
          amount: '2500.00',
          narration: 'UPI to Ramesh',
          score: 70,
          reasons: ['amount exact', '0d apart'],
        },
      ],
    },
  ],
};

const RECONCILIATION: BankReconciliation = {
  ledger_id: 7,
  ledger_name: 'HDFC Current',
  as_of: '2026-04-30',
  book_balance: '15000.00',
  statement_balance: '12500.00',
  statement_balance_source: 'given',
  reconciled_balance: '15000.00',
  unmatched_statement_total: '-2500.00',
  unmatched_book_total: '0.00',
  pair_difference_total: '0.00',
  rows: [
    {
      kind: 'stmt_debit',
      label: 'Payment per bank, not in books',
      date: '2026-04-03',
      amount: '2500.00',
      effect: '-2500.00',
      reference: 'UPI123456',
      narration: 'UPI/Payment to Ramesh',
    },
  ],
  proof: { lhs: '2500.00', rhs: '2500.00', ok: true },
};

interface Recorded {
  method: string;
  url: string;
  body?: string;
}

let calls: Recorded[];

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url, body: init?.body ? String(init.body) : undefined });
    if (url.endsWith('/api/v1/ledgers')) return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/banking/ledgers')) return jsonResponse({ ledgers: BANKS });
    if (url.endsWith('/api/v1/banking/7/lines?status=all')) return jsonResponse(LINE_BOOK);
    if (url.endsWith('/api/v1/banking/7/suggestions?window=7')) return jsonResponse(SUGGESTIONS);
    if (url.endsWith('/api/v1/banking/7/matches') && method === 'POST')
      return jsonResponse({ statement_line_id: 101, voucher_no: 'PYT-2026-27-00001' }, 201);
    if (url.endsWith('/api/v1/banking/matches/101') && method === 'DELETE')
      return jsonResponse({ statement_line_id: 101, unmatched: true });
    if (url.includes('/api/v1/banking/7/reconciliation')) return jsonResponse(RECONCILIATION);
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
  // the voucher screen is up once masters arrive
  await screen.findByLabelText('Line 1 particulars');
  return user;
}

describe('the banking screen (ADR-026)', () => {
  it('Alt+G opens the line book with match state', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}g{/Alt}');
    const heading = await screen.findByRole('button', { name: 'Import…' });
    expect(heading).toBeTruthy();
    // the matched line names its voucher; the withdrawal and deposit
    // land in their own columns
    expect(await screen.findByText('UPI/Payment to Ramesh')).toBeTruthy();
    expect(screen.getByText('RCT-2026-27-00002 (2026-04-05)')).toBeTruthy();
    // the withdrawal and the suggestion both carry 2500.00 — two surfaces,
    // one figure
    expect(screen.getAllByText('2500.00').length).toBe(2);
  });

  it('M posts the one explicit match — suggestions never commit themselves', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}g{/Alt}');
    await screen.findByText('UPI/Payment to Ramesh');
    // the first line is selected by default — its suggestion is listed
    expect(await screen.findByText('PYT-2026-27-00001')).toBeTruthy();
    await user.keyboard('m');
    await waitFor(() => {
      const posted = calls.find(
        (c) => c.method === 'POST' && c.url.endsWith('/api/v1/banking/7/matches'),
      );
      expect(posted).toBeTruthy();
      expect(posted?.body).toContain('"statement_line_id":101');
      expect(posted?.body).toContain('"entry_id":31');
    });
  });

  it('U withdraws a live match by naming the line', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}g{/Alt}');
    await screen.findByText('NEFT Credit from Acme');
    // walk to the matched credit (row 2 — line 102), then withdraw
    await user.keyboard('{ArrowDown}');
    await user.keyboard('u');
    await waitFor(() => {
      const deleted = calls.find(
        (c) => c.method === 'DELETE' && c.url.endsWith('/api/v1/banking/matches/102'),
      );
      expect(deleted).toBeTruthy();
    });
  });

  it('the reconciliation proof renders the engine’s exact verdict', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}g{/Alt}');
    await screen.findByText('UPI/Payment to Ramesh');
    await user.type(
      screen.getByLabelText('Balance per statement (blank = fold)'),
      '12500.00',
    );
    await user.click(screen.getByRole('button', { name: 'Reconcile' }));
    const proof = await screen.findByText(/Proof 2500\.00 = 2500\.00/);
    expect(proof).toBeTruthy();
    expect(screen.getByText('Payment per bank, not in books')).toBeTruthy();
  });
});
