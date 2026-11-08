/**
 * The GST compliance screen (ADR-028, Phase 7): the compliance workbench —
 * import the portal's GSTR-2A/2B download (paste the operator adapter's
 * JSON, preview first — the receipt shows what would land), walk the ITC
 * reconciliation table (matched / mismatched / missing in books / missing in
 * portal, every reason named), read the ITC both sides claim, and take the
 * statutory PDFs (GSTR-1, 3B, 9, the comparison) home. `Alt+T` opens it
 * (T for tax), `Esc` is back to entry. Zero business logic here — every
 * number is the engine's string, the client never sums (Law 3).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { GstDownloadReceipt, GstImportReceipt, GstReconData } from '../api/types';
import { Banner, type Notice } from './Banner';

type LoadState = 'loading' | 'ready' | 'error';
type Kind = '2a' | '2b';

interface GstScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

/** The statutory PDFs the bar's buttons open — a pack key, its label, and
 * whether it folds a whole year instead of a tax month (GSTR-9). */
const PDF_LINKS: { key: string; label: string; year?: boolean }[] = [
  { key: 'gstr1', label: 'GSTR-1' },
  { key: 'gstr3b', label: 'GSTR-3B' },
  { key: 'gstr9', label: 'GSTR-9', year: true },
  { key: 'gst-recon', label: 'Comparison' },
];

/** A tax month (YYYY-MM) or a whole year (YYYY), as the report asks. */
function validPeriod(raw: string, year = false): boolean {
  return year ? /^\d{4}$/.test(raw) : /^\d{4}-(0[1-9]|1[0-2])$/.test(raw);
}

/** imported_at trimmed to seconds — "2026-11-14 09:30:12". */
function when(importedAt: string | null): string {
  return importedAt ? importedAt.slice(0, 19).replace('T', ' ') : '—';
}

export function GstScreen({ onExit, ctxRef }: GstScreenProps) {
  const [downloads, setDownloads] = useState<GstDownloadReceipt[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [gstin, setGstin] = useState('');
  const [kind, setKind] = useState<Kind>('2b');
  const [recon, setRecon] = useState<GstReconData | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [receipt, setReceipt] = useState<GstImportReceipt | null>(null);
  const periodRef = useRef<HTMLInputElement | null>(null);
  const textRef = useRef<HTMLTextAreaElement | null>(null);
  const sourceRef = useRef<HTMLInputElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('gstScreen', () => {
    onExit();
    return true;
  });

  ctxRef.current = { gstOpen: true, importOpen };
  useEffect(
    () => () => {
      ctxRef.current = {};
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
    },
    [ctxRef],
  );

  // ---- the downloads feed — on mount, and again on Refresh ---------------
  const loadDownloads = () => {
    setLoadState('loading');
    api
      .gstDownloads()
      .then((rows) => {
        setDownloads(rows);
        setLoadState('ready');
      })
      .catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not read the downloads' });
        setLoadState('error');
      });
  };
  useEffect(loadDownloads, []);

  /** The registrations the loaded downloads speak of — All, plus each
   * distinct GSTIN in the server's newest-first order. */
  const registrations = useMemo(
    () => [...new Set(downloads.map((d) => d.gstin).filter((g) => g !== ''))],
    [downloads],
  );

  // ---- the comparison ----------------------------------------------------
  function runCompare() {
    const period = periodRef.current?.value.trim() ?? '';
    if (!validPeriod(period)) {
      setNotice({ kind: 'err', text: 'The comparison needs a period like 2026-10 (YYYY-MM).' });
      return;
    }
    api
      .gstRecon(period, kind, gstin || undefined)
      .then((data) => {
        setRecon(data);
        setNotice((n) => (n?.kind === 'ok' ? n : null));
      })
      .catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The comparison was refused' });
      });
  }

  // ---- the import — preview (dry run) first, then the one commit ---------
  async function doImport(dryRun: boolean) {
    const text = textRef.current?.value ?? '';
    const source = sourceRef.current?.value.trim() ?? '';
    try {
      const rec = await api.gstImport(text, source || undefined, dryRun);
      setReceipt(rec);
      if (!dryRun) {
        setImportOpen(false);
        setNotice({
          kind: 'ok',
          text:
            `Imported ${rec.new} new line(s), ${rec.duplicates} duplicate(s) — ` +
            `${rec.kind.toUpperCase()} ${rec.period} for ${rec.gstin} (revision ${rec.revision}).`,
        });
        loadDownloads();
        runCompare();
      }
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The download was refused' });
    }
  }

  // ---- the statutory PDFs — fetch (the book header travels), then a tab --
  async function openPdf(link: { key: string; label: string; year?: boolean }) {
    const raw = periodRef.current?.value.trim() ?? '';
    let period = raw;
    if (link.year) {
      // GSTR-9 folds the financial year: the period's year, else the current
      period = raw.slice(0, 4);
      if (!validPeriod(period, true)) period = String(new Date().getFullYear());
    } else if (!validPeriod(raw)) {
      setNotice({ kind: 'err', text: `${link.label} needs a period like 2026-10 (YYYY-MM).` });
      return;
    }
    const params: Record<string, string> = { period };
    if (gstin) params.gstin = gstin;
    if (link.key === 'gst-recon') params.kind = kind;
    try {
      const blob = await api.packPdf(link.key, params);
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
      const url = URL.createObjectURL(blob);
      objectUrlRef.current = url;
      const opened = window.open(url, '_blank', 'noopener');
      if (!opened) {
        throw new Error('The browser blocked the PDF tab — allow pop-ups for this site and retry');
      }
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not fetch the PDF' });
    }
  }

  const summary = useMemo(() => {
    if (!recon) return null;
    const { counts, itc } = recon;
    const parts = [
      `Matched ${counts.matched} · Mismatched ${counts.mismatched} · ` +
        `Missing in books ${counts.missing_books} · Missing in portal ${counts.missing_portal}`,
      `ITC: books ${itc.books} / portal ${itc.portal} / difference ${itc.difference}`,
    ];
    if (itc.portal_ineligible_rows > 0) {
      parts.push(
        `${itc.portal_ineligible_rows} portal row(s) marked ITC-ineligible ` +
          `(${itc.portal_ineligible_tax} of tax): reverse the claim before the 3B's Table 4.`,
      );
    }
    return parts.join(' — ');
  }, [recon]);

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">GST</span>
        <select aria-label="Registration" value={gstin} onChange={(e) => setGstin(e.target.value)}>
          <option value="">All registrations</option>
          {registrations.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
        <select
          aria-label="Statement kind"
          value={kind}
          onChange={(e) => setKind(e.target.value as Kind)}
        >
          <option value="2b">GSTR-2B</option>
          <option value="2a">GSTR-2A</option>
        </select>
        <input
          ref={periodRef}
          aria-label="Period (YYYY-MM)"
          placeholder="e.g. 2026-10"
          defaultValue={new Date().toISOString().slice(0, 7)}
        />
        <button type="button" onClick={runCompare}>
          Compare
        </button>
        <button
          type="button"
          onClick={() => {
            setReceipt(null);
            setImportOpen(true);
          }}
        >
          Import Download…
        </button>
        <button type="button" onClick={loadDownloads}>
          Refresh
        </button>
      </header>

      <Banner notice={notice} loading={loadState === 'loading'} apiError={false} onRetry={loadDownloads} />

      <main className="gst-area">
        <section aria-label="Imported downloads">
          <h3 className="hint">Imported downloads</h3>
          <table className="outstanding-table" data-testid="gst-downloads">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Period</th>
                <th>GSTIN</th>
                <th>Source</th>
                <th className="num">Lines</th>
                <th>Imported</th>
              </tr>
            </thead>
            <tbody>
              {downloads.map((d) => (
                <tr key={d.id}>
                  <td>{d.kind}</td>
                  <td>{d.period}</td>
                  <td>{d.gstin}</td>
                  <td>{d.source ?? '—'}</td>
                  <td className="num">{d.line_count}</td>
                  <td>{when(d.imported_at)}</td>
                </tr>
              ))}
              {!downloads.length && (
                <tr>
                  <td colSpan={6} className="empty-note">
                    No portal downloads yet — import a GSTR-2A/2B statement to begin.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        {recon && (
          <section aria-label="ITC reconciliation">
            <p className="hint" data-testid="gst-summary">
              {summary}
            </p>
            <table className="outstanding-table" data-testid="gst-recon">
              <thead>
                <tr>
                  <th>State</th>
                  <th>GSTIN</th>
                  <th>Party</th>
                  <th>Document</th>
                  <th>Date</th>
                  <th className="num">Books taxable</th>
                  <th className="num">Portal taxable</th>
                  <th className="num">Books tax</th>
                  <th className="num">Portal tax</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {recon.rows.map((row) => (
                  <tr key={`${row.supplier_gstin}:${row.doc_no}`}>
                    <td>{row.state}</td>
                    <td>{row.supplier_gstin}</td>
                    <td>{row.party}</td>
                    <td>
                      {row.doc_type} {row.doc_no}
                    </td>
                    <td>{row.doc_date}</td>
                    <td className="num">{row.books_taxable ?? '—'}</td>
                    <td className="num">{row.portal_taxable ?? '—'}</td>
                    <td className="num">{row.books_tax ?? '—'}</td>
                    <td className="num">{row.portal_tax ?? '—'}</td>
                    <td>{row.reason || '—'}</td>
                  </tr>
                ))}
                {!recon.rows.length && (
                  <tr>
                    <td colSpan={10} className="empty-note">
                      Nothing to compare — neither the books nor the portal statement holds a
                      document for this period.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </section>
        )}

        <section className="gst-actions" aria-label="Statutory PDFs">
          {PDF_LINKS.map((link) => (
            <button key={link.key} type="button" onClick={() => void openPdf(link)}>
              {link.label}
            </button>
          ))}
        </section>
      </main>

      {importOpen && (
        <div className="modal" role="dialog" aria-label="Import GST portal download">
          <div className="modal-card">
            <h3>Import download — paste the adapter's JSON</h3>
            <textarea
              ref={textRef}
              aria-label="Portal download JSON"
              rows={10}
              placeholder={
                'Paste the portal download — the operator adapter\'s JSON (the lf-gst-2a/1 / ' +
                'lf-gst-2b/1 contract the GST portal file translates to): ' +
                '{"kind": "lf-gst-2b/1", "period": "2026-10", "gstin": "29ABCDE1234F1Z5", ' +
                '"entries": [{ "supplier_gstin": "…", "doc_no": "…", "doc_date": "…", ' +
                '"taxable": …, "igst": …, "cgst": …, "sgst": … }]}'
              }
            />
            <input
              ref={sourceRef}
              aria-label="Source"
              placeholder="a label for the receipt (file name…)"
              maxLength={200}
            />
            {receipt?.dry_run && (
              <p className="hint">
                Preview: {receipt.parsed} parsed, {receipt.new} would import, {receipt.duplicates}{' '}
                duplicates, revision {receipt.revision}.
              </p>
            )}
            <div className="modal-actions">
              <button type="button" onClick={() => void doImport(true)}>
                Preview
              </button>
              <button type="button" className="primary" onClick={() => void doImport(false)}>
                Import
              </button>
              <button type="button" onClick={() => setImportOpen(false)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="sr-only" aria-live="polite">
        GST — {downloads.length} download{downloads.length === 1 ? '' : 's'} imported
      </div>
    </div>
  );
}
