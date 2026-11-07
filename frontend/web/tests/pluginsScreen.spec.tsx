/**
 * The plugins screen (ADR-016): Alt+X opens it, the inventory loads from
 * GET /api/v1/plugins, loaded packs show their channel and trust basis,
 * boot refusals show their reasons, and Esc is back to voucher entry —
 * driven through the real keyboard-kit pipeline with only the network
 * mocked.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { PluginsOut } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

const INVENTORY: PluginsOut = {
  plugins: [
    { name: 'invoicing', version: '1.0.0', api_compat: '0.2', source: 'entry-point', trust: 'trusted' },
    { name: 'statutory', version: '1.0.0', api_compat: '0.6', source: 'entry-point', trust: 'trusted' },
    { name: 'webhook-forwarder', version: '0.1.0', api_compat: '0.2', source: 'drop-in', trust: 'signed' },
    { name: 'hometown-pack', version: '0.4.0', api_compat: '0.3', source: 'drop-in', trust: 'acknowledged' },
  ],
  refused: [
    {
      module: 'rogue',
      reason:
        'no operator acknowledgement in .trust.json and no detached signature (rogue.py.sig) — see `ledgerforge plugins ack` / `install`',
    },
  ],
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
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url });
    if (url.endsWith('/api/v1/ledgers')) return jsonResponse(LEDGERS);
    if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
    if (url.endsWith('/api/v1/plugins')) return jsonResponse(INVENTORY);
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

async function openPlugins(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Alt>}x{/Alt}');
  await screen.findByRole('table', { name: 'Loaded plugins' });
}

describe('the plugins screen (ADR-016)', () => {
  it('Alt+X opens the screen from the gateway; Esc is back to entry', async () => {
    const user = await setup();
    expect(screen.queryByText(/loaded — 4 packs/i)).toBeNull();

    await openPlugins(user);
    expect(screen.getByText(/loaded — 4 packs/i)).toBeVisible();

    await user.keyboard('{Escape}');
    await screen.findByLabelText('Line 1 particulars');
    expect(screen.queryByText(/loaded — 4 packs/i)).toBeNull();
  });

  it('lists every pack with its channel and trust basis', async () => {
    const user = await setup();
    await openPlugins(user);

    const table = await screen.findByRole('table', { name: 'Loaded plugins' });
    expect(table).toHaveTextContent('invoicing');
    expect(table).toHaveTextContent('built-in / pip');
    expect(table).toHaveTextContent('webhook-forwarder');
    expect(table).toHaveTextContent('drop-in');
    expect(table).toHaveTextContent('signed');
    expect(table).toHaveTextContent('acknowledged');
    // the API version floor each pack declares is visible
    expect(table).toHaveTextContent('0.6');
  });

  it('shows the boot refusals with their reasons — the gate at work', async () => {
    const user = await setup();
    await openPlugins(user);

    const refused = await screen.findByRole('table', { name: 'Refused plugins' });
    expect(refused).toHaveTextContent('rogue');
    expect(refused).toHaveTextContent(/no detached signature/);
  });

  it('refresh refetches the inventory', async () => {
    const user = await setup();
    await openPlugins(user);

    const before = calls.filter((c) => c.url.endsWith('/api/v1/plugins')).length;
    await user.click(screen.getByRole('button', { name: /refresh the inventory/i }));
    await waitFor(() =>
      expect(calls.filter((c) => c.url.endsWith('/api/v1/plugins')).length).toBe(before + 1),
    );
  });

  it('the screen is palette-visible like every command', async () => {
    const user = await setup();
    await user.keyboard('{Control>}k{/Control}');
    const palette = await screen.findByRole('dialog');
    expect(palette).toHaveTextContent(/Plugins — loaded packs & trust status/i);
  });
});
