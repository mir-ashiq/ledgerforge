/**
 * The books screen (Phase 1, ADR-009 in the UI): Alt+O opens the company
 * picker; books list with currency/FY/status; Enter switches the active
 * book — from then on every request carries X-Book-Id; C creates a book
 * (seeded with the TallyPrime chart in one action) and selects it; A
 * archives/restores; Esc is back to entry. Driven through the real
 * keyboard-kit pipeline with only the network mocked.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { BookOut, BookSeededOut } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

function book(id: number, name: string, over: Partial<BookOut> = {}): BookOut {
  return {
    id,
    name,
    slug: name.toLowerCase().replace(/\s+/g, '-'),
    base_currency: 'INR',
    fy_start_month: 4,
    is_archived: false,
    created_at: '2026-04-01T10:00:00Z',
    ...over,
  };
}

const BOOKS: BookOut[] = [
  book(1, 'Acme Traders'),
  book(2, 'Beacon Retail', { fy_start_month: 1, created_at: '2026-06-15T10:00:00Z' }),
];

interface Recorded {
  method: string;
  url: string;
  headers: Record<string, string>;
}

let calls: Recorded[];
let booksState: BookOut[];

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => {
  calls = [];
  booksState = BOOKS.map((b) => ({ ...b }));
  window.localStorage.clear();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    const headers = (init?.headers ?? {}) as Record<string, string>;
    calls.push({ method, url, headers });
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/books') && method === 'POST') {
      const payload = JSON.parse(String(init?.body)) as { name: string };
      const created = book(booksState.length + 1, payload.name);
      booksState.push(created);
      const out: BookSeededOut = {
        book: created,
        seeded_groups: 28,
        seeded_ledgers: 7,
        seeded_voucher_types: 10,
      };
      return jsonResponse(out, 201);
    }
    if (url.startsWith('/api/v1/books/') && method === 'PATCH') {
      const id = Number(url.split('/books/')[1]);
      const payload = JSON.parse(String(init?.body)) as { name?: string; is_archived?: boolean };
      booksState = booksState.map((b) =>
        b.id === id ? { ...b, ...payload, name: payload.name ?? b.name } : b,
      );
      return jsonResponse(booksState.find((b) => b.id === id));
    }
    if (url.endsWith('/api/v1/books')) return jsonResponse(booksState);
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

async function openBooks(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}o{/Alt}');
  await screen.findByRole('grid', { name: 'Books' });
}

describe('the books screen (Phase 1, ADR-009)', () => {
  it('Alt+O opens the screen; every book lists with its currency, FY start and status; Esc is back', async () => {
    const user = await setup();
    await openBooks(user);

    const grid = screen.getByRole('grid', { name: 'Books' });
    expect(grid).toHaveTextContent('Acme Traders');
    expect(grid).toHaveTextContent('Beacon Retail');
    expect(grid).toHaveTextContent('INR');
    expect(grid).toHaveTextContent('April'); // FY starts
    expect(grid).toHaveTextContent('January');
    expect(screen.getByText('2 books')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
  });

  it('Enter switches the active book — every request carries X-Book-Id from then on', async () => {
    const user = await setup();
    await openBooks(user);

    // two books, none adopted at boot: the masters calls carried no header
    const bootLedgerCalls = calls.filter((c) => c.url.endsWith('/api/v1/ledgers'));
    expect(bootLedgerCalls.length).toBeGreaterThan(0);
    expect(bootLedgerCalls.every((c) => c.headers['X-Book-Id'] === undefined)).toBe(true);

    await user.keyboard('{Enter}'); // Acme Traders is highlighted
    await screen.findByLabelText('Line 1 particulars'); // back to entry, masters reloaded

    const after = calls
      .filter((c) => c.url.endsWith('/api/v1/ledgers'))
      .filter((c) => c.headers['X-Book-Id'] !== undefined);
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((c) => c.headers['X-Book-Id'] === '1')).toBe(true);

    // the voucher screen's bar names the open company, TallyPrime-style
    expect(screen.getByText(/Voucher Entry — Acme Traders/)).toBeInTheDocument();

    // the choice persists: remembered in localStorage for the next launch
    expect(window.localStorage.getItem('lf.activeBook')).toBe('1');
  });

  it('C creates a book — POST /books — and the new book becomes active', async () => {
    const user = await setup();
    await openBooks(user);

    await user.keyboard('c');
    const dialog = await screen.findByRole('dialog', { name: 'Create a book' });
    await user.type(within(dialog).getByLabelText('Book name'), 'Cyclone Exports');
    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(
        calls.some((c) => c.method === 'POST' && c.url.endsWith('/api/v1/books')),
      ).toBe(true),
    );

    // the created book is selected: the next masters fetch targets book 3
    await screen.findByLabelText('Line 1 particulars');
    await waitFor(() => {
      const toBook3 = calls.filter(
        (c) => c.url.endsWith('/api/v1/ledgers') && c.headers['X-Book-Id'] === '3',
      );
      expect(toBook3.length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/Voucher Entry — Cyclone Exports/)).toBeInTheDocument();
  });

  it('A archives and restores the highlighted book', async () => {
    const user = await setup();
    await openBooks(user);

    await user.keyboard('{ArrowDown}'); // Beacon Retail
    await user.keyboard('a');
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.method === 'PATCH' && c.url.endsWith('/api/v1/books/2'),
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.getByText('archived')).toBeInTheDocument());

    await user.keyboard('a'); // restore
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'PATCH' && c.url.endsWith('/api/v1/books/2')).length).toBe(2),
    );
    await waitFor(() => expect(screen.queryByText('archived')).toBeNull());
  });

  it('the screen is palette-visible like every command', async () => {
    const user = await setup();
    await user.keyboard('{Control>}k{/Control}');
    const palette = await screen.findByRole('dialog');
    expect(palette).toHaveTextContent(/Books — companies: select, create, archive/i);
  });
});
