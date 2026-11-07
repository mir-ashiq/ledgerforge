/**
 * The outstandings screen (ADR-023, Phase 2): bills receivable / bills
 * payable with ageing, plus every party's on-account pool. `Alt+I` opens
 * it; `R`/`P` (or the tab buttons) switch the side, `↑/↓` walk the rows,
 * `F` filters by party (type-ahead over the report's own parties), `Esc`
 * is back to entry. Every number is the engine's string — the client never
 * sums (Law 3); the ageing footer proves the bucket totals against the
 * report total.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { OutstandingBill, OutstandingReport } from '../api/types';
import { Banner, type Notice } from './Banner';

type LoadState = 'loading' | 'ready' | 'error';
type Side = 'dr' | 'cr';

interface OutstandingsScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const BUCKET_ORDER = ['not due', '0-30', '31-60', '61-90', '90+'] as const;
const SIDE_LABEL: Record<Side, string> = { dr: 'Bills receivable', cr: 'Bills payable' };

export function OutstandingsScreen({ onExit, ctxRef }: OutstandingsScreenProps) {
  const [side, setSide] = useState<Side>('dr');
  const [report, setReport] = useState<OutstandingReport | null>(null);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [partyFilter, setPartyFilter] = useState('');
  const [filterFocus, setFilterFocus] = useState(false);
  const [index, setIndex] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);
  const filterRef = useRef<HTMLInputElement | null>(null);

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('outstandingsScreen', () => {
    onExit();
    return true;
  });

  ctxRef.current = { outstandingsOpen: true, filterFocus };
  useEffect(() => () => {
    ctxRef.current = {};
  }, [ctxRef]);

  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .outstanding(side)
      .then((data) => {
        if (cancelled) return;
        setReport(data);
        setLoadState('ready');
        setIndex(0);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not load the outstanding book' });
        setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [side]);

  const bills = useMemo(() => {
    const rows = report?.bills ?? [];
    if (!partyFilter.trim()) return rows;
    const needle = partyFilter.trim().toLowerCase();
    return rows.filter((b) => b.party.toLowerCase().includes(needle));
  }, [report, partyFilter]);

  useEffect(() => {
    setIndex((i) => Math.min(i, Math.max(0, bills.length - 1)));
  }, [bills.length]);

  useEffect(() => {
    document
      .querySelector(`[data-bill-row="${index}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [index]);

  const inList = () => ctxRef.current.outstandingsOpen === true && !filterFocus;

  useCommand(
    { id: 'outstanding.receivable', title: 'Outstanding: receivable side', run: () => setSide('dr') },
    'R',
    inList as () => boolean,
  );
  useCommand(
    { id: 'outstanding.payable', title: 'Outstanding: payable side', run: () => setSide('cr') },
    'P',
    inList as () => boolean,
  );
  useCommand(
    {
      id: 'outstanding.up',
      title: 'Outstanding: previous bill',
      run: () => setIndex((i) => Math.max(0, i - 1)),
    },
    'ArrowUp',
    inList as () => boolean,
  );
  useCommand(
    {
      id: 'outstanding.down',
      title: 'Outstanding: next bill',
      run: () => setIndex((i) => Math.min(bills.length - 1, i + 1)),
    },
    'ArrowDown',
    inList as () => boolean,
  );
  useCommand(
    {
      id: 'outstanding.filter',
      title: 'Outstanding: filter by party',
      run: () => filterRef.current?.focus(),
    },
    'F',
    inList as () => boolean,
  );

  const onAccountRows = useMemo(
    () => Object.entries(report?.on_account ?? {}).filter(
      ([party]) =>
        !partyFilter.trim() || party.toLowerCase().includes(partyFilter.trim().toLowerCase()),
    ),
    [report, partyFilter],
  );

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">{SIDE_LABEL[side]}</span>
        <div className="types" role="tablist" aria-label="Outstanding side">
          {(['dr', 'cr'] as const).map((s) => (
            <button
              key={s}
              type="button"
              role="tab"
              aria-selected={s === side}
              className={s === side ? 'on' : ''}
              onClick={() => setSide(s)}
            >
              {SIDE_LABEL[s]} <kbd>{s === 'dr' ? 'R' : 'P'}</kbd>
            </button>
          ))}
        </div>
        <span className="spacer" />
        <input
          ref={filterRef}
          className="filter-input"
          aria-label="Filter by party"
          placeholder="Filter by party (F)"
          value={partyFilter}
          onChange={(e) => setPartyFilter(e.target.value)}
          onFocus={() => setFilterFocus(true)}
          onBlur={() => setFilterFocus(false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === 'Escape') filterRef.current?.blur();
          }}
        />
      </header>

      <Banner notice={notice} loading={loadState === 'loading'} apiError={false} onRetry={() => setSide(side)} />

      <main className="outstanding-area">
        {loadState === 'ready' && report && (
          <>
            <table className="outstanding-table" aria-label={`${SIDE_LABEL[side]} with ageing`}>
              <thead>
                <tr>
                  <th>Party</th>
                  <th>Ref</th>
                  <th>Opened</th>
                  <th>Due</th>
                  <th className="num">Outstanding</th>
                  <th>Ageing</th>
                  <th className="num">Rate</th>
                </tr>
              </thead>
              <tbody>
                {bills.map((bill: OutstandingBill, i: number) => (
                  <tr key={`${bill.party}:${bill.ref}`} data-bill-row={i} className={i === index ? 'sel' : ''}>
                    <td>{bill.party}</td>
                    <td>{bill.ref}</td>
                    <td>{bill.opened_on}</td>
                    <td>{bill.due_on}</td>
                    <td className="num">{bill.outstanding}</td>
                    <td>{bill.bucket}</td>
                    <td className="num">{bill.interest_annual ? `${bill.interest_annual}%` : '—'}</td>
                  </tr>
                ))}
                {onAccountRows.map(([party, amount]) => (
                  <tr key={`${party}:on-account`}>
                    <td>{party}</td>
                    <td>on account</td>
                    <td>—</td>
                    <td>—</td>
                    <td className="num">{amount}</td>
                    <td>—</td>
                    <td className="num">—</td>
                  </tr>
                ))}
                {!bills.length && !onAccountRows.length && (
                  <tr>
                    <td colSpan={7} className="empty-note">
                      No {SIDE_LABEL[side].toLowerCase()} outstanding as of {report.as_of}.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            <div className="outstanding-footer" role="status">
              {BUCKET_ORDER.map((bucket) => (
                <span key={bucket} className="ageing-chip">
                  {bucket}: <strong>{report.ageing[bucket] ?? '0.00'}</strong>
                </span>
              ))}
              <span className="ageing-chip total">
                Total: <strong>{report.total}</strong>
              </span>
            </div>
            <p className="hint">
              {bills.length} open bill{bills.length === 1 ? '' : 's'} · on-account pools shown as
              their own rows · ageing by due date as of {report.as_of} — bills + on-account equal
              each party's ledger balance (ADR-023).
            </p>
          </>
        )}
        {loadState === 'error' && <p className="hint">The outstanding book could not be read.</p>}
      </main>

      <div className="sr-only" aria-live="polite">
        {SIDE_LABEL[side]} loaded
      </div>
    </div>
  );
}
