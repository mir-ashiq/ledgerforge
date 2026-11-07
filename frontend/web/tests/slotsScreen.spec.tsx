/**
 * The slots screen + host (ADR-019): Alt+U opens the picker, GET /api/v1/slots
 * lists the declared dashboards, Enter opens one in a hardened sandbox
 * (sandbox="allow-scripts", served by the host for inline slots), and the
 * §5.5 protocol runs over postMessage with only the network mocked:
 * register-commands makes slot commands palette-reachable and host-bound,
 * lf-slot:request is relayed as a GET on the versioned API and answered
 * with lf-slot:response, and lf-slot:close pops back to the list.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { SlotsOut } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];

const SLOTS: SlotsOut = {
  slots: [
    {
      key: 'balance-glance',
      title: 'Balance glance — trial balance in a sandbox',
      plugin: 'example-slot',
      source: 'entry-point',
      trust: 'trusted',
      url: null,
    },
    {
      key: 'external-dash',
      title: 'External dash',
      plugin: 'acme-pack',
      source: 'drop-in',
      trust: 'signed',
      url: 'https://dash.example/panel',
    },
  ],
};

const TRIAL_BALANCE = {
  as_of: '2026-11-07',
  ledgers: [],
  totals: { dr: '100.00', cr: '100.00', difference: '0' },
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
    if (url.endsWith('/api/v1/slots')) return jsonResponse(SLOTS);
    if (url.includes('/api/v1/reports/trial-balance')) return jsonResponse(TRIAL_BALANCE);
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

function postedToFrame(messages: unknown[]): { type: string }[] {
  return messages.map((m) => m as { type: string });
}

describe('the slots screen (ADR-019)', () => {
  it('Alt+U opens the picker listing every declared slot with pack and trust', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}{u}{/Alt}');
    expect(await screen.findByText(/sandboxed plugin dashboards/i)).toBeInTheDocument();
    expect(screen.getByText('Balance glance — trial balance in a sandbox')).toBeInTheDocument();
    expect(screen.getByText('External dash')).toBeInTheDocument();
    expect(screen.getByText('example-slot')).toBeInTheDocument();
    expect(screen.getByText('signed drop-in')).toBeInTheDocument();
    // Esc is the universal back
    await user.keyboard('{Escape}');
    expect(await screen.findByLabelText('Line 1 particulars')).toBeInTheDocument();
  });

  it('Enter opens the inline slot in a hardened sandbox served by the host', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}{u}{/Alt}');
    const row = await screen.findByTestId('slot-row-balance-glance');
    row.focus();
    await user.keyboard('{Enter}');
    const frame = await screen.findByTitle('Balance glance — trial balance in a sandbox');
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
    expect(frame.getAttribute('src')).toBe('/api/v1/slots/balance-glance');
    // Esc pops the slot back to the list, a second Esc leaves the screen
    await user.keyboard('{Escape}');
    expect(screen.getByTestId('slot-row-balance-glance')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(await screen.findByLabelText('Line 1 particulars')).toBeInTheDocument();
  });

  it('an external-url slot embeds its own URL, still sandboxed', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}{u}{/Alt}');
    const row = await screen.findByTestId('slot-row-external-dash');
    row.focus();
    await user.keyboard('{ArrowDown}{Enter}');
    const frame = await screen.findByTitle('External dash');
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
    expect(frame.getAttribute('src')).toBe('https://dash.example/panel');
  });

  it('runs the §5.5 protocol: commands register, keys route back, requests relay', async () => {
    const user = await setup();
    await user.keyboard('{Alt>}{u}{/Alt}');
    const row = await screen.findByTestId('slot-row-balance-glance');
    row.focus();
    await user.keyboard('{Enter}');
    const frame = (await screen.findByTitle(
      'Balance glance — trial balance in a sandbox',
    )) as HTMLIFrameElement;

    // jsdom never fires iframe onLoad — spy on everything the host posts
    // into the sandbox first, then drive the host the way the load event would
    const posted: unknown[] = [];
    const contentWindow = frame.contentWindow!;
    vi.spyOn(contentWindow, 'postMessage').mockImplementation(((message: unknown) => {
      posted.push(message);
    }) as typeof contentWindow.postMessage);
    frame.dispatchEvent(new Event('load'));
    expect(postedToFrame(posted)).toContainEqual(
      expect.objectContaining({ type: 'lf-slot:init', slot: 'balance-glance', apiBase: '/api/v1' }),
    );

    // the sandbox registers its commands; the host surfaces them in the
    // palette (§5.5) and binds their keys while the slot is open
    window.dispatchEvent(
      new MessageEvent('message', {
        source: contentWindow,
        data: {
          type: 'lf-slot:register-commands',
          commands: [
            { id: 'example-slot.refresh', title: 'Balance glance: refresh', key: 'R' },
            { id: 'example-slot.rows', title: 'Balance glance: show/hide rows' },
          ],
        },
      }),
    );
    await waitFor(() => {
      expect(screen.getByText(/Balance glance: refresh · Balance glance: show\/hide rows/)).toBeInTheDocument();
    });
    await user.keyboard('{Control>}k{/Control}');
    expect(await screen.findByText('Balance glance: refresh')).toBeInTheDocument();
    await user.keyboard('{Escape}'); // close the palette (slot still open)

    // a host-bound key routes back into the sandbox as lf-slot:command
    await user.keyboard('r');
    await waitFor(() => {
      expect(posted).toContainEqual({ type: 'lf-slot:command', id: 'example-slot.refresh' });
    });

    // the relay: the sandbox asks for a versioned-API GET, the host performs
    // it with its own session and answers with the parsed body
    calls = [];
    window.dispatchEvent(
      new MessageEvent('message', {
        source: contentWindow,
        data: { type: 'lf-slot:request', id: 'tb', path: '/reports/trial-balance?fast=true' },
      }),
    );
    await waitFor(() => {
      expect(calls).toContainEqual({
        method: 'GET',
        url: '/api/v1/reports/trial-balance?fast=true',
      });
    });
    await waitFor(() => {
      expect(posted).toContainEqual({
        type: 'lf-slot:response',
        id: 'tb',
        ok: true,
        status: 200,
        body: TRIAL_BALANCE,
      });
    });

    // the relay refuses anything that is not a versioned-API GET path
    window.dispatchEvent(
      new MessageEvent('message', {
        source: contentWindow,
        data: { type: 'lf-slot:request', id: 'evil', path: '//evil.example/x' },
      }),
    );
    await waitFor(() => {
      expect(posted).toContainEqual(
        expect.objectContaining({ type: 'lf-slot:response', id: 'evil', ok: false }),
      );
    });

    // lf-slot:close pops the slot even if focus is trapped in the sandbox
    window.dispatchEvent(
      new MessageEvent('message', { source: contentWindow, data: { type: 'lf-slot:close' } }),
    );
    await waitFor(() => {
      expect(screen.getByTestId('slot-row-balance-glance')).toBeInTheDocument();
    });
  });

  it('empty state: no slots declared', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/ledgers')) return jsonResponse(LEDGERS);
      if (url.endsWith('/api/v1/groups')) return jsonResponse(GROUPS);
      if (url.endsWith('/api/v1/slots')) return jsonResponse({ slots: [] });
      throw new Error(`unmocked ${url}`);
    }) as typeof fetch;
    const user = await setup();
    await user.keyboard('{Alt>}{u}{/Alt}');
    expect(await screen.findByText(/no pack declares a slot yet/i)).toBeInTheDocument();
  });
});
