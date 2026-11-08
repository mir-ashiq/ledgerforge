/**
 * The inventory screen (Phase 6, ADR-027): Alt+V opens the stock depth —
 * items with tracking modes and re-order levels, the positions fold with
 * godown/batch splits, the expiry/shortfall/godown/batch reports one key
 * away (1..5), the new-item form posts a master. (Alt+V — Alt+N is the
 * voucher screen's new-draft.) Driven through the real
 * keyboard pipeline with only the network mocked; every number is the
 * engine's string (Law 3).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { StockBom, StockItemMaster, StockPositionReport } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];
const BOOKS = [
  { id: 1, name: 'My Company', slug: 'default', base_currency: 'INR', fy_start_month: 4, is_archived: false, created_at: '2026-04-01T10:00:00Z' },
];

const ITEMS: StockItemMaster[] = [
  {
    id: 1,
    name: 'Amoxycillin 500',
    unit: 'strip',
    track_batches: true,
    track_serials: false,
    reorder_level: '40.00000000',
    reorder_quantity: '100.00000000',
    notes: null,
    price_count: 2,
    bom_count: 0,
  },
  {
    id: 2,
    name: 'Pumps',
    unit: null,
    track_batches: false,
    track_serials: true,
    reorder_level: null,
    reorder_quantity: null,
    notes: null,
    price_count: 0,
    bom_count: 1,
  },
];

const BOMS: StockBom[] = [
  {
    id: 3,
    name: 'Desk-STD',
    output_item_id: 2,
    output: 'Pumps',
    output_qty: '2.00000000',
    notes: null,
    components: [
      { id: 1, component_item_id: 1, component: 'Amoxycillin 500', qty_per_output: '6.00000000' },
    ],
  },
];

const POSITIONS: StockPositionReport = {
  as_of: '2026-11-08',
  items: [
    {
      item: 'Amoxycillin 500',
      qty: '20',
      value: '250.00',
      avg_cost: '12.5000',
      serial_count: 0,
      godowns: { 'Main location': '14', Annex: '6' },
      batches: [
        { batch: 'AMX-114', qty: '10', expiry: '2027-03-31', godowns: { 'Main location': '10' } },
        { batch: 'AMX-207', qty: '10', expiry: '2026-12-15', godowns: { Annex: '6', 'Main location': '4' } },
      ],
    },
  ],
  total_value: '250.00',
};

const EXPIRY = {
  as_of: '2026-11-08',
  rows: [
    {
      item: 'Amoxycillin 500',
      batch: 'AMX-207',
      expiry: '2026-12-15',
      days_left: '37',
      bucket: '<=60d',
      qty: '10',
      value: '125.00',
    },
  ],
  bucket_totals: { expired: '0.00', '<=30d': '0.00', '<=60d': '125.00', '<=90d': '0.00', '>90d': '0.00' },
};

let fetches: string[];
let posts: { url: string; body: unknown }[];

beforeEach(() => {
  fetches = [];
  posts = [];
  window.localStorage.clear();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    fetches.push(url);
    if (init?.method === 'POST') posts.push({ url, body: JSON.parse(String(init.body ?? '{}')) });
    if (url.endsWith('/api/v1/ledgers')) return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/books')) return jsonResponse(BOOKS);
    if (url.includes('/api/v1/inventory/items') && init?.method === 'POST') {
      const body = posts.at(-1)?.body as Record<string, unknown>;
      return jsonResponse({ id: 99, ...body }, 201);
    }
    if (url.endsWith('/api/v1/inventory/items')) return jsonResponse(ITEMS);
    if (url.endsWith('/api/v1/inventory/boms')) return jsonResponse(BOMS);
    if (url.includes('/reports/pack/stock-position')) return jsonResponse(POSITIONS);
    if (url.includes('/reports/pack/stock-expiry')) return jsonResponse(EXPIRY);
    if (url.includes('/reports/pack/stock-shortfall')) return jsonResponse({ as_of: '2026-11-08', rows: [] });
    if (url.includes('/reports/pack/stock-godown')) return jsonResponse({ as_of: '2026-11-08', rows: [], godown_totals: {} });
    if (url.includes('/reports/pack/stock-batch')) return jsonResponse({ as_of: '2026-11-08', rows: [] });
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
  await user.keyboard('{Alt>}v{/Alt}');
  await waitFor(() => expect(screen.getByText('Inventory')).toBeTruthy());
  return user;
}

describe('the inventory screen (Phase 6, ADR-027)', () => {
  it('Alt+V opens the items tab with the masters and their depth flags', async () => {
    const user = await setup();
    const table = await screen.findByTestId('inventory-items');
    expect(table.textContent).toContain('Amoxycillin 500');
    expect(table.textContent).toContain('strip');
    // NUMERIC scale is storage, not display
    expect(table.textContent).toContain('100');
    // the BOMs tab renders the recipe
    await user.keyboard('b');
    const boms = await screen.findByTestId('inventory-boms');
    expect(boms.textContent).toContain('Desk-STD');
    expect(boms.textContent).toContain('Amoxycillin 500 × 6');
  });

  it('S opens the stock tab: positions with the godown and batch split', async () => {
    const user = await setup();
    await user.keyboard('s');
    const table = await screen.findByTestId('inventory-positions');
    expect(table.textContent).toContain('Amoxycillin 500');
    expect(table.textContent).toContain('Main location: 14');
    expect(table.textContent).toContain('Annex: 6');
    expect(table.textContent).toContain('AMX-114: 10');
    expect(table.textContent).toContain('250.00');
  });

  it('2 switches to the expiry ageing view — FEFO’s evidence, same fold', async () => {
    const user = await setup();
    await user.keyboard('s');
    await screen.findByTestId('inventory-positions');
    await user.keyboard('2');
    const expiry = await screen.findByTestId('inventory-expiry');
    expect(expiry.textContent).toContain('AMX-207');
    expect(expiry.textContent).toContain('2026-12-15');
    expect(expiry.textContent).toContain('<=60d');
    expect(fetches.some((url) => url.includes('stock-expiry'))).toBe(true);
  });

  it('the new-item form posts a master and refreshes the table', async () => {
    const user = await setup();
    const name = await screen.findByPlaceholderText('New stock item (name)');
    await user.type(name, 'Serum');
    await user.type(screen.getByPlaceholderText('Unit (of measure)'), 'vial');
    await user.type(screen.getByPlaceholderText('Re-order level'), '25');
    await user.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() =>
      expect(posts).toEqual([
        {
          url: expect.stringContaining('/api/v1/inventory/items'),
          body: { name: 'Serum', unit: 'vial', reorder_level: '25' },
        },
      ]),
    );
    expect(await screen.findByText('Stock item Serum created')).toBeTruthy();
  });
});
