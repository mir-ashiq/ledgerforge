/**
 * The GST compliance screen (Phase 7, ADR-028): Alt+T opens the compliance
 * workbench — the downloads table with its honest provenance (imported_at
 * trimmed to seconds), Compare walks the reconciliation's four states with
 * the ITC both sides claim (the engine's exact strings, Law 3), the
 * registration picker offers All plus each receipt's GSTIN, and the import
 * flow previews (dry run) before it commits. Driven through the real
 * keyboard pipeline with only the network mocked.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { GstDownloadReceipt, GstReconData } from '../src/api/types';

const LEDGERS = [
  { id: 1, name: 'Cash', code: null, group_id: 10, group_name: 'Cash-in-Hand', is_system: true },
];
const GROUPS = [
  { id: 1, name: 'Primary', code: null, parent_id: null, nature: 'liability', affects_gross: false, is_root: true },
];
const BOOKS = [
  { id: 1, name: 'My Company', slug: 'default', base_currency: 'INR', fy_start_month: 4, is_archived: false, created_at: '2026-04-01T10:00:00Z' },
];

const DOWNLOADS: GstDownloadReceipt[] = [
  {
    id: 2,
    kind: '2b',
    period: '2026-10',
    gstin: '29ABCDE1234F1Z5',
    source: 'GSTR2B_Oct.json',
    line_count: 3,
    imported_at: '2026-11-14T09:30:12+00:00',
  },
  {
    id: 1,
    kind: '2b',
    period: '2026-09',
    gstin: '27AAECS1234F1Z1',
    source: null,
    line_count: 1,
    imported_at: '2026-10-16T18:05:44',
  },
];

const RECON: GstReconData = {
  period: '2026-10',
  kind: '2b',
  gstin: null,
  counts: { matched: 1, mismatched: 1, missing_books: 1, missing_portal: 1 },
  unmatchable_books: 1,
  itc: {
    books: '2880.00',
    portal: '3960.00',
    difference: '-1080.00',
    portal_ineligible_rows: 1,
    portal_ineligible_tax: '90.00',
  },
  rows: [
    {
      state: 'matched',
      supplier_gstin: '29ABCDE1234F1Z5',
      party: 'Sun Pharma',
      doc_type: 'INV',
      doc_no: 'SP-101',
      doc_date: '2026-10-04',
      books_taxable: '10000.00',
      books_tax: '1800.00',
      portal_taxable: '10000.00',
      portal_tax: '1800.00',
      reason: '',
    },
    {
      state: 'mismatched',
      supplier_gstin: '29ABCDE1234F1Z5',
      party: 'Med Distributors',
      doc_type: 'INV',
      doc_no: 'MD-55',
      doc_date: '2026-10-12',
      books_taxable: '5000.00',
      books_tax: '900.00',
      portal_taxable: '4000.00',
      portal_tax: '720.00',
      reason: 'taxable 5000.00↔4000.00; tax 900.00↔720.00',
    },
    {
      state: 'missing_books',
      supplier_gstin: '27AAECS1234F1Z1',
      party: 'Coastal Chemicals',
      doc_type: 'INV',
      doc_no: 'CC-9',
      doc_date: '2026-10-22',
      books_taxable: null,
      books_tax: null,
      portal_taxable: '8000.00',
      portal_tax: '1440.00',
      reason: 'the portal drafted it, the books do not hold it',
    },
    {
      state: 'missing_portal',
      supplier_gstin: '29ABCDE1234F1Z5',
      party: 'Sun Pharma',
      doc_type: 'DN',
      doc_no: 'SP-DN-2',
      doc_date: '2026-10-28',
      books_taxable: '1000.00',
      books_tax: '180.00',
      portal_taxable: null,
      portal_tax: null,
      reason: 'the books claim it, the portal has not drafted it',
    },
  ],
};

/** One portal download in the lf-gst-2b/1 adapter contract (what the
 * operator's tooling pastes into the import box). */
const DOWNLOAD_TEXT = JSON.stringify({
  kind: 'lf-gst-2b/1',
  period: '2026-10',
  gstin: '29ABCDE1234F1Z5',
  entries: [
    {
      supplier_gstin: '29ABCDE1234F1Z5',
      doc_no: 'SP-101',
      doc_date: '2026-10-04',
      taxable: 10000,
      igst: 0,
      cgst: 900,
      sgst: 900,
    },
  ],
});

const SOURCE_LABEL = '2b-oct-r2.json';

const PREVIEW_RECEIPT = {
  kind: '2b',
  period: '2026-10',
  gstin: '29ABCDE1234F1Z5',
  source: SOURCE_LABEL,
  parsed: 4,
  new: 3,
  duplicates: 1,
  revision: 2,
  dry_run: true,
};

let downloads: GstDownloadReceipt[];
let fetches: string[];
let posts: { url: string; body: unknown }[];

beforeEach(() => {
  downloads = [...DOWNLOADS];
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
    if (url.endsWith('/api/v1/gst/portal/downloads')) return jsonResponse({ downloads });
    if (url.endsWith('/api/v1/gst/portal/import')) {
      const body = posts.at(-1)?.body as { dry_run?: boolean };
      if (!body?.dry_run) {
        // the real import lands a receipt — the refreshed table shows it
        downloads = [
          {
            id: 3,
            kind: '2b',
            period: '2026-10',
            gstin: '29ABCDE1234F1Z5',
            source: SOURCE_LABEL,
            line_count: 3,
            imported_at: '2026-11-15T08:00:00',
          },
          ...downloads,
        ];
      }
      return jsonResponse(
        body?.dry_run ? PREVIEW_RECEIPT : { ...PREVIEW_RECEIPT, dry_run: false, batch_id: 3 },
        201,
      );
    }
    if (url.includes('/reports/pack/gst-recon')) return jsonResponse(RECON);
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
  // the voucher screen is up once masters arrive
  await screen.findByLabelText('Line 1 particulars');
  await user.keyboard('{Alt>}t{/Alt}');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Compare' })).toBeTruthy());
  return user;
}

describe('the GST compliance screen (Phase 7, ADR-028)', () => {
  it('Alt+T opens the workbench with the downloads table — provenance to the second', async () => {
    await setup();
    const table = await screen.findByTestId('gst-downloads');
    // both receipts, the newest first
    expect(table.textContent).toContain('GSTR2B_Oct.json');
    expect(table.textContent).toContain('29ABCDE1234F1Z5');
    expect(table.textContent).toContain('27AAECS1234F1Z1');
    expect(table.textContent).toContain('2026-09');
    // imported_at is trimmed to seconds, the T becomes a space
    expect(table.textContent).toContain('2026-11-14 09:30:12');
    expect(table.textContent).not.toContain('2026-11-14T09:30:12');
    // an absent source is an honest dash
    expect(table.textContent).toContain('—');
  });

  it('the registration select offers All registrations plus each receipt GSTIN', async () => {
    await setup();
    await screen.findByTestId('gst-downloads');
    expect(screen.getByRole('option', { name: 'All registrations' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '29ABCDE1234F1Z5' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '27AAECS1234F1Z1' })).toBeTruthy();
  });

  it('Compare renders every state row and the ITC both sides claim', async () => {
    const user = await setup();
    await screen.findByTestId('gst-downloads');
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    const table = await screen.findByTestId('gst-recon');
    // every bucket is visible — the engine's own verdict strings
    for (const state of ['matched', 'mismatched', 'missing_books', 'missing_portal']) {
      expect(table.textContent).toContain(state);
    }
    // the document column joins type and number; every reason is named
    expect(table.textContent).toContain('INV SP-101');
    expect(table.textContent).toContain('the portal drafted it, the books do not hold it');
    expect(table.textContent).toContain('the books claim it, the portal has not drafted it');
    // the summary: counts + the exact ITC figures (the client never sums)
    const summary = await screen.findByTestId('gst-summary');
    expect(summary.textContent).toContain('Matched 1');
    expect(summary.textContent).toContain('Mismatched 1');
    expect(summary.textContent).toContain('Missing in books 1');
    expect(summary.textContent).toContain('Missing in portal 1');
    expect(summary.textContent).toContain('books 2880.00');
    expect(summary.textContent).toContain('portal 3960.00');
    expect(summary.textContent).toContain('difference -1080.00');
    // the portal's own ITC-ineligible verdict is named with its tax
    expect(summary.textContent).toContain('1 portal row(s) marked ITC-ineligible (90.00 of tax)');
    // the fold was asked for the 2B statement of the current period
    expect(
      fetches.some(
        (url) => url.includes('/reports/pack/gst-recon') && url.includes('kind=2b'),
      ),
    ).toBe(true);
  });

  it('the import flow previews (dry run) before it commits, then refreshes', async () => {
    const user = await setup();
    await screen.findByTestId('gst-downloads');
    await user.click(screen.getByRole('button', { name: 'Import Download…' }));
    const dialog = await screen.findByRole('dialog', { name: 'Import GST portal download' });
    fireEvent.change(screen.getByLabelText('Portal download JSON'), {
      target: { value: DOWNLOAD_TEXT },
    });
    fireEvent.change(screen.getByLabelText('Source'), { target: { value: SOURCE_LABEL } });
    // the preview is the dry run — the receipt names what would land
    await user.click(within(dialog).getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/3 would import/)).toBeTruthy();
    expect(screen.getByText(/revision 2/)).toBeTruthy();
    expect(posts).toEqual([
      {
        url: expect.stringContaining('/api/v1/gst/portal/import'),
        body: { text: DOWNLOAD_TEXT, source: SOURCE_LABEL, dry_run: true },
      },
    ]);
    // the real import commits with the same text and source
    await user.click(within(dialog).getByRole('button', { name: 'Import' }));
    await waitFor(() =>
      expect(posts).toEqual([
        {
          url: expect.stringContaining('/api/v1/gst/portal/import'),
          body: { text: DOWNLOAD_TEXT, source: SOURCE_LABEL, dry_run: true },
        },
        {
          url: expect.stringContaining('/api/v1/gst/portal/import'),
          body: { text: DOWNLOAD_TEXT, source: SOURCE_LABEL, dry_run: false },
        },
      ]),
    );
    // the receipt banner answers, and the downloads table refreshed with it
    expect(
      await screen.findByText(
        'Imported 3 new line(s), 1 duplicate(s) — 2B 2026-10 for 29ABCDE1234F1Z5 (revision 2).',
      ),
    ).toBeTruthy();
    const table = await screen.findByTestId('gst-downloads');
    expect(table.textContent).toContain(SOURCE_LABEL);
    // the comparison re-ran against the fresh download
    expect(fetches.filter((url) => url.includes('/reports/pack/gst-recon')).length).toBeGreaterThan(
      0,
    );
  });
});
