/**
 * The daybook browser (ADR-015): every posted voucher, browsable,
 * reprintable and — since Phase 1 — correctable. `Alt+B` opens it; `↑/↓`
 * (or `j/k`) walk the list, the selection's entries preview on the right,
 * `Enter`/`Ctrl+P` print the selected voucher through the same endpoint the
 * payslip and voucher screens use (ADR-013), `Ctrl+R` posts the mirrored
 * reversal (Law 2's one correction path — history is never edited, only
 * reversed), `Esc` is back to entry — the same keyboard contract as every
 * LedgerForge screen. Amounts are displayed exactly as the engine computed
 * them: strings, never client arithmetic (Law 3).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { ContextPredicate } from '@ledgerforge/keyboard-kit';
import { api } from '../api/client';
import type { VoucherOut } from '../api/types';
import { ConfirmOverlay } from './ConfirmOverlay';
import { DOC_KIND_LABEL, docKindOf, matchesText, money, printEmployeeArg } from '../daybook/model';
import { VOUCHER_TYPES } from '../voucher/model';

type LoadState = 'loading' | 'error' | 'ready' | 'empty';

interface DaybookScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

// the list owns the arrows only while the filter row does not have focus
// (a focused date input or select must keep its native keys)
const inDaybook: ContextPredicate = (ctx) =>
  ctx.scopeId === 'daybookScreen' && ctx.filterFocus !== true;

export function DaybookScreen({ onExit, ctxRef }: DaybookScreenProps) {
  const [vouchers, setVouchers] = useState<readonly VoucherOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [type, setType] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [text, setText] = useState('');
  const [index, setIndex] = useState(0);
  const [filterFocus, setFilterFocus] = useState(false);
  const [reload, setReload] = useState(0);
  const [printing, setPrinting] = useState(false);
  const [confirmReverse, setConfirmReverse] = useState(false);
  const [reversing, setReversing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [announce, setAnnounce] = useState('');
  const objectUrlRef = useRef<string | null>(null);
  const lastSelectedIdRef = useRef<number | null>(null);

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('daybookScreen', () => {
    onExit();
    return true;
  });

  // live flags for keymap predicates (read at keydown time, always fresh)
  ctxRef.current = { daybookOpen: true, filterFocus, dialogOpen: confirmReverse };
  useEffect(
    () => () => {
      ctxRef.current = {};
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // the server-side browse (type + date range); the text filter is client-side
  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .vouchers({ type: type || undefined, from: from || undefined, to: to || undefined })
      .then((list) => {
        if (cancelled) return;
        setVouchers(list);
        setLoadState(list.length ? 'ready' : 'empty');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [type, from, to, reload]);

  const visible = useMemo(
    () => vouchers.filter((v) => matchesText(v, text)),
    [vouchers, text],
  );
  const selected = visible[index] ?? null;

  // keep the same voucher selected across refetches and filter changes,
  // clamping when it falls out of the list
  useEffect(() => {
    const lastId = lastSelectedIdRef.current;
    if (lastId !== null) {
      const at = visible.findIndex((v) => v.id === lastId);
      setIndex(at >= 0 ? at : 0);
    } else if (visible.length) {
      setIndex(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);
  lastSelectedIdRef.current = selected?.id ?? null;

  // the selected row stays in view when the arrows walk past the fold
  useEffect(() => {
    document
      .querySelector(`[data-row="${index}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [index]);

  const move = useCallback(
    (delta: number) => {
      setIndex((i) => Math.min(visible.length - 1, Math.max(0, i + delta)));
    },
    [visible.length],
  );

  const print = useCallback(async () => {
    const voucher = selected;
    if (!voucher || printing) return;
    const employeeArg = printEmployeeArg(voucher);
    if (employeeArg === 'many') {
      const hint =
        'Payroll run with several employees — its payslips print from the Payslips screen (Alt+P), which picks the employee first';
      setNote(hint);
      setAnnounce(hint);
      return;
    }
    setPrinting(true);
    setNote(null);
    try {
      const blob = await api.voucherPdf(voucher.id, employeeArg ?? undefined);
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
      const url = URL.createObjectURL(blob);
      objectUrlRef.current = url;
      const opened = window.open(url, '_blank', 'noopener');
      if (!opened) {
        throw new Error('The browser blocked the PDF tab — allow pop-ups for this site and retry');
      }
      setAnnounce(`Printing ${voucher.voucher_no}`);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Could not fetch the PDF';
      setNote(message);
      setAnnounce(message);
    } finally {
      setPrinting(false);
    }
  }, [selected, printing]);

  // the reversal — Law 2's one correction path, from the UI (Phase 1):
  // a confirmed Ctrl+R posts the mirrored voucher; idempotent on retry
  const reverse = useCallback(async () => {
    const voucher = selected;
    if (!voucher || reversing || confirmReverse) return;
    if (voucher.reversal_of !== null) {
      const hint = 'This voucher is itself a reversal — reversals cannot be reversed; post a fresh voucher instead';
      setNote(hint);
      setAnnounce(hint);
      return;
    }
    setConfirmReverse(true);
  }, [selected, reversing, confirmReverse]);

  const doReverse = useCallback(async () => {
    const voucher = selected;
    if (!voucher) return;
    setConfirmReverse(false);
    setReversing(true);
    setNote(null);
    try {
      const reversal = await api.reverseVoucher(voucher.id);
      setNote(
        `Reversed ${voucher.voucher_no} — the mirrored voucher posted as ${reversal.voucher_no} (net effect exactly zero)`,
      );
      setAnnounce(`Reversal posted as ${reversal.voucher_no}`);
      setReload((n) => n + 1);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'The reversal failed';
      setNote(message);
      setAnnounce(message);
    } finally {
      setReversing(false);
    }
  }, [selected]);

  // --- commands -------------------------------------------------------------
  useCommand(
    { id: 'daybook.up', title: 'Daybook: previous voucher', run: () => move(-1) },
    'ArrowUp',
    inDaybook,
  );
  useCommand(
    { id: 'daybook.down', title: 'Daybook: next voucher', run: () => move(1) },
    'ArrowDown',
    inDaybook,
  );
  useCommand(
    { id: 'daybook.upVim', title: 'Daybook: previous voucher (k)', run: () => move(-1) },
    'k',
    inDaybook,
  );
  useCommand(
    { id: 'daybook.downVim', title: 'Daybook: next voucher (j)', run: () => move(1) },
    'j',
    inDaybook,
  );
  useCommand(
    {
      id: 'daybook.print',
      title: 'Print selected voucher',
      run: () => void print(),
    },
    'Primary+P',
    inDaybook,
  );
  useCommand(
    {
      id: 'daybook.printEnter',
      title: 'Print selected voucher (Enter)',
      run: () => void print(),
    },
    'Enter',
    inDaybook,
  );
  useCommand(
    {
      id: 'daybook.reverse',
      title: 'Reverse the selected voucher (Law 2 correction)',
      run: () => void reverse(),
    },
    'Primary+R',
    inDaybook,
  );

  // --- render -----------------------------------------------------------------
  const typeLabel = (code: string) =>
    VOUCHER_TYPES.find((t) => t.code === code)?.name ?? code;

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Daybook — posted vouchers</span>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={onExit}>
          Voucher entry <kbd>Esc</kbd>
        </button>
      </header>

      <main className="daybook-body">
        <section className="daybook-list-col" aria-label="Voucher browse">
          <div
            className="daybook-filters"
            onFocusCapture={() => setFilterFocus(true)}
            onBlurCapture={() => setFilterFocus(false)}
          >
            <label>
              Type
              <select aria-label="Voucher type" value={type} onChange={(e) => setType(e.target.value)}>
                <option value="">All</option>
                {VOUCHER_TYPES.map((t) => (
                  <option key={t.code} value={t.code}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              From
              <input type="date" aria-label="From date" value={from} onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label>
              To
              <input type="date" aria-label="To date" value={to} onChange={(e) => setTo(e.target.value)} />
            </label>
            <label className="daybook-find">
              Find
              <input
                type="search"
                aria-label="Find vouchers"
                placeholder="no, narration, ledger…"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="daybook-reload"
              aria-label="Refresh the list"
              onClick={() => setReload((n) => n + 1)}
            >
              Refresh
            </button>
            <span className="daybook-count" aria-live="polite">
              {visible.length} voucher{visible.length === 1 ? '' : 's'}
            </span>
          </div>

          {loadState === 'loading' && (
            <p className="payslip-note" role="status">
              Loading posted vouchers…
            </p>
          )}
          {loadState === 'error' && (
            <p className="payslip-note bad" role="alert">
              Cannot reach the engine — is it running? (make dev)
            </p>
          )}
          {loadState === 'empty' && (
            <p className="payslip-note" role="status">
              No vouchers posted yet. Post one from the entry screen (
              <code>Ctrl+A</code>) — it appears here the moment it is posted.
            </p>
          )}
          {loadState === 'ready' && visible.length === 0 && (
            <p className="payslip-note" role="status">
              Nothing matches these filters — widen the dates, the type or the find text.
            </p>
          )}

          {visible.length > 0 && (
            <div className="daybook-list" role="grid" aria-label="Posted vouchers">
              <div className="daybook-row daybook-head" role="row">
                <span role="columnheader">Date</span>
                <span role="columnheader">Vch No</span>
                <span role="columnheader">Type</span>
                <span role="columnheader">Particulars</span>
                <span role="columnheader" className="num">
                  Amount
                </span>
                <span role="columnheader">Doc</span>
              </div>
              {visible.map((v, i) => {
                const kind = docKindOf(v);
                return (
                  <div
                    key={v.id}
                    role="row"
                    aria-selected={i === index}
                    data-row={i}
                    className={`daybook-row${i === index ? ' on' : ''}`}
                    onClick={() => setIndex(i)}
                    onDoubleClick={() => void print()}
                  >
                    <span role="gridcell">{v.voucher_date}</span>
                    <span role="gridcell" className="mono">
                      {v.voucher_no}
                    </span>
                    <span role="gridcell">{typeLabel(v.voucher_type)}</span>
                    <span role="gridcell" className="daybook-narr">
                      {v.narration || v.entries[0]?.ledger_name || '—'}
                    </span>
                    <span role="gridcell" className="num mono">
                      {money(v.amount)}
                    </span>
                    <span role="gridcell" className="daybook-chips">
                      {kind && <span className="daybook-chip">{DOC_KIND_LABEL[kind]}</span>}
                      {v.reversal_of !== null && <span className="daybook-chip rev">reversal</span>}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <aside className="daybook-detail" aria-label="Selected voucher">
          {selected ? (
            <div className="payslip-card daybook-card">
              <div className="payslip-card-head">
                <strong>{selected.voucher_no}</strong>
                <span>
                  {selected.voucher_date} · {typeLabel(selected.voucher_type)}
                </span>
              </div>
              {selected.narration && <p className="daybook-narration">{selected.narration}</p>}
              <table className="daybook-entries" aria-label="Voucher entries">
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">Ledger</th>
                    <th scope="col">Dr/Cr</th>
                    <th scope="col" className="num">
                      Amount
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {selected.entries.map((e) => (
                    <tr key={e.line_no}>
                      <td>{e.line_no}</td>
                      <td>{e.ledger_name}</td>
                      <td>{e.dc === 'D' ? 'Dr' : 'Cr'}</td>
                      <td className="num mono">{money(e.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="overlay-actions daybook-actions">
                <button
                  type="button"
                  className="payslip-print daybook-print"
                  onClick={() => void print()}
                  disabled={printing}
                >
                  {printing ? 'Fetching…' : 'Print / reprint'} <kbd>Ctrl+P</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => void reverse()}
                  disabled={reversing || selected.reversal_of !== null}
                  title={
                    selected.reversal_of !== null
                      ? 'This voucher is itself a reversal — it cannot be reversed'
                      : 'Post the mirrored reversal — history is never edited (Law 2)'
                  }
                >
                  {reversing ? 'Posting…' : 'Reverse'} <kbd>Ctrl+R</kbd>
                </button>
              </div>
              <p className="payslip-note">
                Reprints are identical: posted entries are immutable (Law 2). The PDF opens
                in a new tab — print or save it from there.
              </p>
            </div>
          ) : (
            <p className="payslip-note">Select a voucher — ↑↓ move, Enter prints.</p>
          )}
          {note && (
            <p className="payslip-note bad" role="alert">
              {note}
            </p>
          )}
        </aside>
      </main>

      <footer className="daybook-hud" aria-hidden="true">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> / <kbd>j</kbd>
          <kbd>k</kbd> move
        </span>
        <span>
          <kbd>Enter</kbd> / <kbd>Ctrl+P</kbd> print
        </span>
        <span>
          <kbd>Ctrl+R</kbd> reverse
        </span>
        <span>
          <kbd>Esc</kbd> back
        </span>
      </footer>

      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      {confirmReverse && selected && (
        <ConfirmOverlay
          message={`Reverse ${selected.voucher_no} (${money(selected.amount)})? A mirrored voucher posts today and the pair nets to exactly zero — the original is never edited (Law 2).`}
          confirmLabel="Post the reversal"
          cancelLabel="Keep the voucher"
          onConfirm={() => void doReverse()}
          onCancel={() => setConfirmReverse(false)}
        />
      )}
    </div>
  );
}
