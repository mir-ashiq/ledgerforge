/**
 * Command palette acceptance (keyboard-ux §8 phase 3): Ctrl+K opens the
 * palette, Enter runs, Ctrl+R rebinds (capture mode can never trigger the
 * command it redefines), Alt+R resets, and user bindings survive remounts
 * through the localStorage store.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import { keymap } from '@ledgerforge/keyboard-kit/react';
import { clearPersistedKeymap } from '../src/keymapStore';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
  { id: 2, name: 'Local Sales', code: null, group_id: 20, group_name: 'Sales Accounts', is_system: false },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
  { id: 9, name: 'Suspense A/c', code: null, parent_id: 1, nature: 'liability', affects_gross: false, is_root: false },
  { id: 10, name: 'Cash-in-Hand', code: null, parent_id: 1, nature: 'asset', affects_gross: false, is_root: false },
  { id: 20, name: 'Sales Accounts', code: null, parent_id: 1, nature: 'income', affects_gross: true, is_root: false },
];

interface Recorded {
  method: string;
  url: string;
}

let calls: Recorded[];

beforeEach(() => {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url });
    const ok = (body: unknown, status = 200): Response =>
      ({ ok: status < 400, status, json: async () => body }) as Response;
    if (url.endsWith('/api/v1/ledgers') && method === 'GET') return ok(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return ok(GROUPS);
    if (url.endsWith('/api/v1/ledgers') && method === 'POST') {
      return ok({ id: 99, name: 'New', code: null, group_id: 9, group_name: 'Suspense A/c', is_system: false }, 201);
    }
    if (url.endsWith('/api/v1/vouchers') && method === 'POST') {
      return ok({ id: 7, uuid: 'u', voucher_no: 'SAL-1', voucher_type: 'SAL', voucher_date: '2026-11-06', narration: null, reversal_of: null, posted_at: 'now', client_token: 't', replayed: false, entries: [] }, 201);
    }
    throw new Error(`unmocked ${method} ${url}`);
  }) as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  keymap.resetAllKeybindings();
  clearPersistedKeymap();
});

async function boot() {
  const user = userEvent.setup();
  const utils = render(<App />);
  // masters prefetch lands → the ledger cell can filter 'Cash'
  await user.keyboard('C');
  await screen.findByRole('option', { name: /Cash/ });
  await user.keyboard('{Backspace}');
  return { user, ...utils };
}

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Control>}k{/Control}');
  return screen.findByRole('dialog', { name: 'Command palette' });
}

const dateDialogIsOpen = () => screen.queryByRole('dialog', { name: 'Change voucher date' }) !== null;

describe('command palette — acceptance (keyboard-ux §8 phase 3)', () => {
  it('Ctrl+K opens the palette listing commands with their live keys', async () => {
    const { user } = await boot();
    await openPalette(user);

    const accept = await screen.findByRole('option', { name: /Accept voucher/ });
    expect(accept.textContent).toContain('Ctrl+A');
    expect(screen.getByRole('option', { name: /Command palette/ }).textContent).toContain('Ctrl+K');

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());
  });

  it('filters as you type and Enter runs the highlighted command', async () => {
    const { user } = await boot();
    await openPalette(user);

    await user.keyboard('date');
    const row = await screen.findByRole('option', { name: /Change voucher date/ });
    expect(row.getAttribute('aria-selected')).toBe('true');
    // the filter narrowed: no voucher-type rows left
    expect(screen.queryByRole('option', { name: /Voucher type/ })).toBeNull();

    await user.keyboard('{Enter}');
    await screen.findByRole('dialog', { name: 'Change voucher date' });
    expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull();
    await user.keyboard('{Escape}'); // close the date dialog
  });

  it('Ctrl+R rebinds: new combo fires, old combo is inert, badge says custom', async () => {
    const { user } = await boot();
    await openPalette(user);
    await user.keyboard('date');

    await user.keyboard('{Control>}r{/Control}');
    expect(screen.getByRole('dialog', { name: 'Command palette' }).textContent).toContain('Press the new keys');
    await user.keyboard('{Control>}j{/Control}');

    const row = await screen.findByRole('option', { name: /Change voucher date/ });
    expect(row.textContent).toContain('Ctrl+J');
    expect(row.textContent).toContain('custom');

    await user.keyboard('{Escape}'); // close palette
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());

    await user.keyboard('{F2}');
    expect(dateDialogIsOpen()).toBe(false); // old combo dead
    await user.keyboard('{Control>}j{/Control}');
    expect(dateDialogIsOpen()).toBe(true); // new combo live
    await user.keyboard('{Escape}'); // close date dialog
  });

  it('capture mode never triggers commands: Ctrl+A while capturing rebinds, not posts', async () => {
    const { user } = await boot();
    await openPalette(user);
    await user.keyboard('accept'); // 'Accept voucher' ranks first
    expect(screen.getByRole('option', { name: /^Accept voucher/ }).getAttribute('aria-selected')).toBe('true');

    await user.keyboard('{Control>}r{/Control}');
    await user.keyboard('{Control>}a{/Control}'); // the combo that posts vouchers

    const note = await screen.findByRole('status');
    expect(note.textContent).toContain('rebound');
    expect(calls.filter((c) => c.method === 'POST' && c.url.endsWith('/vouchers'))).toHaveLength(0);
    await user.keyboard('{Escape}');
  });

  it('Escape during capture cancels the rebind but keeps the palette open', async () => {
    const { user } = await boot();
    await openPalette(user);
    await user.keyboard('date');
    await user.keyboard('{Control>}r{/Control}');

    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('cancelled');
    expect(screen.getByRole('option', { name: /Change voucher date/ }).textContent).toContain('F2');

    await user.keyboard('{Escape}'); // now closes the palette
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());
  });

  it('plain keys are refused while capturing (they are for typing)', async () => {
    const { user } = await boot();
    await openPalette(user);
    await user.keyboard('date');
    await user.keyboard('{Control>}r{/Control}');

    await user.keyboard('x');
    const note = await screen.findByRole('status');
    expect(note.textContent).toContain('Plain letter');
    // still capturing: the search box still carries the capture prompt
    const search = screen.getByLabelText('Search commands') as HTMLInputElement;
    expect(search.placeholder).toContain('Press new keys');
    expect(search.readOnly).toBe(true);
    await user.keyboard('{Escape}');
    await user.keyboard('{Escape}');
  });

  it('rebindings persist to the store and still work after a remount', async () => {
    const first = await boot();
    await openPalette(first.user);
    await first.user.keyboard('date');
    await first.user.keyboard('{Control>}r{/Control}');
    await first.user.keyboard('{Control>}j{/Control}');
    await first.user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());

    // the store carries the snapshot (this is what survives a real reload)
    expect(JSON.parse(localStorage.getItem('ledgerforge.keymap.v1') ?? '{}')).toEqual({
      'voucher.changeDate': 'Ctrl+J',
    });

    first.unmount();
    const second = await boot();
    await second.user.keyboard('{Control>}j{/Control}');
    expect(dateDialogIsOpen()).toBe(true);
    await second.user.keyboard('{Escape}');
  });

  it('Alt+R resets a rebound command to its shipped binding', async () => {
    const { user } = await boot();
    // rebind first
    await openPalette(user);
    await user.keyboard('date');
    await user.keyboard('{Control>}r{/Control}');
    await user.keyboard('{Control>}j{/Control}');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());

    // now reset it from the palette
    await openPalette(user);
    await user.keyboard('date');
    await user.keyboard('{Alt>}r{/Alt}');
    const row = await screen.findByRole('option', { name: /Change voucher date/ });
    expect(row.textContent).toContain('F2');
    expect(row.textContent).not.toContain('custom');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeNull());

    await user.keyboard('{F2}');
    expect(dateDialogIsOpen()).toBe(true);
    await user.keyboard('{Escape}');
    expect(JSON.parse(localStorage.getItem('ledgerforge.keymap.v1') ?? '{}')).toEqual({});
  });

  it('while the palette is open, the voucher screen’s keys are inert', async () => {
    const { user } = await boot();
    await openPalette(user);

    await user.keyboard('{F2}');
    expect(dateDialogIsOpen()).toBe(false);
    await user.keyboard('{Alt>}d{/Alt}'); // would delete a grid line
    expect(screen.queryByRole('dialog', { name: 'Command palette' })).toBeTruthy(); // palette untouched

    await user.keyboard('{Escape}');
  });
});
