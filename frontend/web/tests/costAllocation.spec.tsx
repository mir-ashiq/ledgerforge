/**
 * The cost-allocation dialog (Phase 4, ADR-025): Alt+K — the per-line
 * centre splits at entry time. The law under test: every line is fully
 * allocated or left unallocated (the remainder is always visible in the
 * lines table), a partial split cannot be armed, and Ctrl+A posts the
 * voucher with the splits riding each line's cost_allocations — the
 * engine's 422 keeps the draft intact. Driven through the real keyboard
 * pipeline with only the network mocked; amounts asserted as strings
 * end-to-end (Law 3).
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
  { id: 7, name: 'Rent', code: null, group_id: 20, group_name: 'Indirect Expenses', is_system: false },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
  { id: 20, name: 'Indirect Expenses', code: null, parent_id: 1, nature: 'expense', affects_gross: false, is_root: false },
];
const COST_CENTRES = [
  { id: 31, name: 'Primary', code: null, parent_id: null, category_id: null, category_name: null, is_root: true },
  { id: 32, name: 'Mumbai', code: null, parent_id: 31, category_id: 22, category_name: 'Location', is_root: false },
  { id: 33, name: 'Pune', code: null, parent_id: 31, category_id: 22, category_name: 'Location', is_root: false },
];
const COST_CATEGORIES = [
  { id: 21, name: 'Primary', parent_id: null, is_root: true },
  { id: 22, name: 'Location', parent_id: 21, is_root: false },
];

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
    if (url.endsWith('/api/v1/cost-centres')) return jsonResponse(COST_CENTRES);
    if (url.endsWith('/api/v1/cost-categories')) return jsonResponse(COST_CATEGORIES);
    if (url.endsWith('/api/v1/books')) return jsonResponse([
      { id: 1, name: 'Test Co', slug: 'default', base_currency: 'INR', fy_start_month: 4, is_archived: false },
    ]);
    if (url.endsWith('/api/v1/vouchers') && method === 'POST') {
      postedBodies.push(body as Record<string, unknown>);
      return jsonResponse(
        {
          id: 9,
          uuid: 'u-9',
          voucher_no: 'JV-2026-27-00001',
          voucher_type: 'JV',
          voucher_date: '2026-11-08',
          amount: '250.00',
          narration: null,
          reversal_of: null,
          posted_at: '2026-11-08T10:00:00Z',
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

/** A journal: Rent 250 Dr on line 1, Cash 250 Cr on line 2. */
async function typeJournal(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('Rent{Enter}'); // picker accepts -> debit cell
  await user.keyboard('250{Enter}'); // debit -> credit cell
  await user.keyboard('{Enter}'); // skip the empty credit -> line 2's ledger
  await user.keyboard('Cash{Enter}'); // picker accepts -> debit cell
  await user.keyboard('{Enter}'); // skip the empty debit -> credit cell
  await user.keyboard('250'); // credit; the cursor rests here
}

async function openCostDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}k{/Alt}');
  // the masters load lazily on the first open, then the dialog appears
  await screen.findByRole('dialog', { name: 'Allocate to cost centres' });
}

describe('cost allocation dialog (ADR-025, Phase 4)', () => {
  it('Alt+K opens the dialog with every line, the remainder always visible', async () => {
    const user = await setup();
    await typeJournal(user);
    await openCostDialog(user);

    const linesTable = screen.getByRole('table', { name: 'Draft lines' });
    const rows = within(linesTable).getAllByRole('row');
    // header + Rent + Cash: both lines show amount vs allocated 0 vs remainder
    expect(rows[1]!.textContent).toContain('Rent');
    expect(rows[2]!.textContent).toContain('Cash');
    // amount, allocated and remainder all visible on the picked line:
    // 250 | 0 | 250 (the remainder is the amount while nothing is split)
    expect(rows[1]!.textContent).toBe('RentDebit2500250');
  });

  it('a full split arms and rides the line on Ctrl+A', async () => {
    const user = await setup();
    await typeJournal(user);
    await openCostDialog(user);

    await user.click(screen.getByRole('button', { name: 'Add split' }));
    const centreSelect = screen.getByLabelText('Cost centre') as HTMLSelectElement;
    expect(centreSelect.options.length).toBe(2); // Mumbai + Pune, no root
    await user.selectOptions(centreSelect, '32');
    await user.clear(screen.getByLabelText('Split amount 1'));
    await user.type(screen.getByLabelText('Split amount 1'), '250');
    await user.click(screen.getByRole('button', { name: /Arm allocations/ }));

    // the chip in the bar shows the arming; Ctrl+A posts with the split
    await waitFor(() => expect(screen.getByTitle('Edit the armed cost-centre splits').textContent).toContain('1 of 2'));
    await user.keyboard('{Control>}a{/Control}');
    await waitFor(() => expect(postedBodies.length).toBe(1));
    const entries = postedBodies[0]!.entries as Array<Record<string, unknown>>;
    expect(entries[0]!.cost_allocations).toEqual([{ centre_id: 32, amount: '250' }]);
    expect(entries[1]!.cost_allocations).toBeUndefined(); // unallocated rides nothing
  });

  it('a partial split cannot be armed — the law, explained inline', async () => {
    const user = await setup();
    await typeJournal(user);
    await openCostDialog(user);

    await user.click(screen.getByRole('button', { name: 'Add split' }));
    await user.clear(screen.getByLabelText('Split amount 1'));
    await user.type(screen.getByLabelText('Split amount 1'), '100');
    await user.click(screen.getByRole('button', { name: /Arm allocations/ }));

    const error = await screen.findByRole('alert');
    expect(error.textContent).toContain('fully allocated or left unallocated');
    expect(postedBodies.length).toBe(0);
    // the lines table still shows the 150 remainder against the picked line
    const linesTable = screen.getByRole('table', { name: 'Draft lines' });
    const picked = within(linesTable).getAllByRole('row')[1]!;
    expect(picked.textContent).toContain('150'); // the remainder, always visible
  });

  it('Leave unallocated is the explicit honest state', async () => {
    const user = await setup();
    await typeJournal(user);
    await openCostDialog(user);

    await user.click(screen.getByRole('button', { name: 'Add split' }));
    await user.clear(screen.getByLabelText('Split amount 1'));
    await user.type(screen.getByLabelText('Split amount 1'), '250');
    await user.click(screen.getByRole('button', { name: 'Leave unallocated' }));
    await user.click(screen.getByRole('button', { name: /Arm allocations/ }));

    await waitFor(() => expect(screen.getByTitle('Edit the armed cost-centre splits').textContent).toContain('0 of 2'));
    await user.keyboard('{Control>}a{/Control}');
    await waitFor(() => expect(postedBodies.length).toBe(1));
    const entries = postedBodies[0]!.entries as Array<Record<string, unknown>>;
    expect(entries[0]!.cost_allocations).toBeUndefined();
  });

  it('a split across two centres arms exactly', async () => {
    const user = await setup();
    await typeJournal(user);
    await openCostDialog(user);

    await user.click(screen.getByRole('button', { name: 'Add split' }));
    await user.selectOptions(screen.getByLabelText('Cost centre'), '32');
    await user.clear(screen.getByLabelText('Split amount 1'));
    await user.type(screen.getByLabelText('Split amount 1'), '100');
    await user.click(screen.getByRole('button', { name: 'Add split' }));
    const selects = screen.getAllByLabelText('Cost centre');
    await user.selectOptions(selects[1]!, '33');
    await user.clear(screen.getByLabelText('Split amount 2'));
    await user.type(screen.getByLabelText('Split amount 2'), '150');
    await user.click(screen.getByRole('button', { name: /Arm allocations/ }));

    // 100 + 150 = 250: armed without a word, and the post carries both
    await waitFor(() => expect(screen.getByTitle('Edit the armed cost-centre splits').textContent).toContain('1 of 2'));
    await user.keyboard('{Control>}a{/Control}');
    await waitFor(() => expect(postedBodies.length).toBe(1));
    const entries = postedBodies[0]!.entries as Array<Record<string, unknown>>;
    expect(entries[0]!.cost_allocations).toEqual([
      { centre_id: 32, amount: '100' },
      { centre_id: 33, amount: '150' },
    ]);
  });
});
