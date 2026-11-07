/**
 * The periods screen (ADR-008 in the UI): fiscal periods, locking and the
 * financial-year close — the native window's period page, in the web UI.
 * `Alt+E` opens it; `↑/↓` walk the periods, `L` locks (freeze for review;
 * postings into a locked period get 409), `U` unlocks (refused once a close
 * sweep exists), `P` previews exactly what closing would post — every
 * income/expense ledger's zeroing line plus the Profit & Loss A/c balancing
 * line — and `Enter` confirms the close from the preview: the sweep posts as
 * an FYC voucher dated the next period's first day, and the period is
 * closed, final. `N` creates a period, `Esc` is back to entry.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { ContextPredicate } from '@ledgerforge/keyboard-kit';
import { api } from '../api/client';
import type { FyCloseReportOut, PeriodOut } from '../api/types';
import { money } from '../daybook/model';

type LoadState = 'loading' | 'error' | 'ready';

interface PeriodsScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const inPeriods: ContextPredicate = (ctx) =>
  ctx.scopeId === 'periodsScreen' && ctx.dialogOpen !== true;

const STATUS_LABEL: Record<PeriodOut['status'], string> = {
  open: 'open',
  locked: 'locked',
  closed: 'closed',
};

export function PeriodsScreen({ onExit, ctxRef }: PeriodsScreenProps) {
  const [periods, setPeriods] = useState<readonly PeriodOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [index, setIndex] = useState(0);
  const [reload, setReload] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [preview, setPreview] = useState<{ period: PeriodOut; report: FyCloseReportOut } | null>(
    null,
  );
  const [closing, setClosing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [announce, setAnnounce] = useState('');

  useFocusScope('periodsScreen', () => {
    if (showCreate || preview) {
      setShowCreate(false);
      setPreview(null);
      return false; // the inner scopes' Esc handlers manage themselves
    }
    onExit();
    return true;
  });

  ctxRef.current = { periodsOpen: true, dialogOpen: showCreate || preview !== null };
  useEffect(
    () => () => {
      ctxRef.current = {};
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .periods()
      .then((list) => {
        if (cancelled) return;
        setPeriods(list);
        setLoadState('ready');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const selected = periods[index] ?? null;

  const fail = useCallback((err: unknown, what: string) => {
    const message = err instanceof Error ? err.message : `${what} failed`;
    setNote(message);
    setAnnounce(message);
  }, []);

  const refresh = useCallback(() => setReload((n) => n + 1), []);

  const move = useCallback(
    (delta: number) => {
      setIndex((i) => Math.min(periods.length - 1, Math.max(0, i + delta)));
    },
    [periods.length],
  );

  useEffect(() => {
    document.querySelector(`[data-period-row="${index}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [index]);

  const lock = useCallback(
    async (period: PeriodOut) => {
      setNote(null);
      try {
        const updated = await api.lockPeriod(period.id);
        setPeriods((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
        setAnnounce(`Locked ${updated.name}`);
      } catch (err) {
        fail(err, 'Lock');
      }
    },
    [fail],
  );

  const unlock = useCallback(
    async (period: PeriodOut) => {
      setNote(null);
      try {
        const updated = await api.unlockPeriod(period.id);
        setPeriods((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
        setAnnounce(`Unlocked ${updated.name}`);
      } catch (err) {
        fail(err, 'Unlock');
      }
    },
    [fail],
  );

  const showPreview = useCallback(
    async (period: PeriodOut) => {
      setNote(null);
      try {
        const report = await api.closePreviewPeriod(period.id);
        setPreview({ period, report });
        setAnnounce(`Close preview for ${period.name}: net profit ${money(report.net_profit)}`);
      } catch (err) {
        fail(err, 'Close preview');
      }
    },
    [fail],
  );

  const close = useCallback(
    async (period: PeriodOut) => {
      setClosing(true);
      setNote(null);
      try {
        const result = await api.closePeriod(period.id);
        setPreview(null);
        setPeriods((prev) => prev.map((p) => (p.id === result.period.id ? result.period : p)));
        setAnnounce(
          result.sweep_posted && result.sweep_voucher
            ? `Closed ${result.period.name} — the P&L sweep posted as ${result.sweep_voucher.voucher_no}`
            : `Closed ${result.period.name}`,
        );
      } catch (err) {
        fail(err, 'Close');
      } finally {
        setClosing(false);
      }
    },
    [fail],
  );

  // --- commands (the list owns the keys while no dialog is open) -------------
  useCommand(
    { id: 'periods.up', title: 'Periods: previous period', run: () => move(-1) },
    'ArrowUp',
    inPeriods,
  );
  useCommand(
    { id: 'periods.down', title: 'Periods: next period', run: () => move(1) },
    'ArrowDown',
    inPeriods,
  );
  useCommand(
    {
      id: 'periods.lock',
      title: 'Periods: lock the highlighted period',
      run: () => {
        if (selected) void lock(selected);
      },
    },
    'l',
    inPeriods,
  );
  useCommand(
    {
      id: 'periods.unlock',
      title: 'Periods: unlock the highlighted period',
      run: () => {
        if (selected) void unlock(selected);
      },
    },
    'u',
    inPeriods,
  );
  useCommand(
    {
      id: 'periods.preview',
      title: 'Periods: preview the close of the highlighted period',
      run: () => {
        if (selected) void showPreview(selected);
      },
    },
    'p',
    inPeriods,
  );
  useCommand(
    {
      id: 'periods.create',
      title: 'Periods: create a period',
      run: () => setShowCreate(true),
    },
    'n',
    inPeriods,
  );
  useCommand(
    { id: 'periods.refresh', title: 'Periods: refresh the list', run: refresh },
    'F5',
    inPeriods,
  );

  // --- render -----------------------------------------------------------------
  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Periods — lock &amp; the financial-year close</span>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={onExit}>
          Voucher entry <kbd>Esc</kbd>
        </button>
      </header>

      <main className="plugins-body">
        {loadState === 'loading' && (
          <p className="payslip-note" role="status">
            Loading periods…
          </p>
        )}
        {loadState === 'error' && (
          <p className="payslip-note bad" role="alert">
            Cannot reach the engine — is it running? (make dev)
          </p>
        )}
        {loadState === 'ready' && periods.length === 0 && (
          <section className="payslip-card plugins-card" aria-label="No periods yet">
            <div className="payslip-card-head">
              <strong>No periods yet</strong>
            </div>
            <p className="payslip-note">
              A period is a non-overlapping date range (typically the fiscal year).
              Posting is gated by period state: <strong>open</strong> accepts postings,{' '}
              <strong>locked</strong> freezes the dates for review, <strong>closed</strong>{' '}
              is terminal — the P&L has been swept to the Profit &amp; Loss A/c and the
              history beneath it is immutable. Create the first fiscal year to begin.
            </p>
            <button type="button" className="payslip-print" onClick={() => setShowCreate(true)}>
              Create the first period <kbd>N</kbd>
            </button>
          </section>
        )}

        {periods.length > 0 && (
          <section className="payslip-card plugins-card" aria-label="Periods">
            <div className="payslip-card-head">
              <strong>
                {periods.length} period{periods.length === 1 ? '' : 's'}
              </strong>
              <button
                type="button"
                className="daybook-reload"
                aria-label="Refresh the periods list"
                onClick={refresh}
              >
                Refresh
              </button>
            </div>
            <div className="daybook-list" role="grid" aria-label="Periods">
              <div className="daybook-row daybook-head" role="row">
                <span role="columnheader">Name</span>
                <span role="columnheader">From</span>
                <span role="columnheader">To</span>
                <span role="columnheader">Status</span>
                <span role="columnheader">Closed by</span>
              </div>
              {periods.map((p, i) => (
                <div
                  key={p.id}
                  role="row"
                  aria-selected={i === index}
                  data-period-row={i}
                  className={`daybook-row${i === index ? ' on' : ''}`}
                  onClick={() => setIndex(i)}
                >
                  <span role="gridcell" className="daybook-narr">
                    {p.name}
                  </span>
                  <span role="gridcell" className="mono">
                    {p.starts_on}
                  </span>
                  <span role="gridcell" className="mono">
                    {p.ends_on}
                  </span>
                  <span role="gridcell" className="daybook-chips">
                    <span className={`daybook-chip${p.status === 'closed' ? ' rev' : ''}`}>
                      {STATUS_LABEL[p.status]}
                    </span>
                  </span>
                  <span role="gridcell">{p.closed_by ?? '—'}</span>
                </div>
              ))}
            </div>
            {selected && (
              <div className="overlay-actions periods-actions">
                <button
                  type="button"
                  onClick={() => void lock(selected)}
                  disabled={selected.status !== 'open'}
                >
                  Lock <kbd>L</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => void unlock(selected)}
                  disabled={selected.status !== 'locked'}
                >
                  Unlock <kbd>U</kbd>
                </button>
                <button
                  type="button"
                  onClick={() => void showPreview(selected)}
                  disabled={selected.status === 'closed'}
                >
                  Close preview <kbd>P</kbd>
                </button>
                <button type="button" onClick={() => setShowCreate(true)}>
                  New period <kbd>N</kbd>
                </button>
              </div>
            )}
            <p className="payslip-note">
              Lock freezes a period's dates for review — postings into it get 409. Close
              is lock-then-sweep-then-final: the P&amp;L zeroes into the Profit &amp; Loss
              A/c as an FYC voucher dated the next period's first day, which must already
              exist and be open.
            </p>
          </section>
        )}

        {note && (
          <p className="payslip-note bad" role="alert">
            {note}
          </p>
        )}
      </main>

      <footer className="daybook-hud" aria-hidden="true">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> move · <kbd>L</kbd> lock · <kbd>U</kbd> unlock
        </span>
        <span>
          <kbd>P</kbd> close preview · <kbd>N</kbd> new period
        </span>
        <span>
          <kbd>Esc</kbd> back
        </span>
      </footer>

      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      {showCreate && (
        <PeriodDialog
          onCancel={() => setShowCreate(false)}
          onDone={(period) => {
            setShowCreate(false);
            setPeriods((prev) =>
              [...prev.filter((p) => p.id !== period.id), period].sort((a, b) =>
                a.starts_on.localeCompare(b.starts_on),
              ),
            );
            setAnnounce(`Created ${period.name}`);
          }}
        />
      )}

      {preview && (
        <ClosePreviewDialog
          period={preview.period}
          report={preview.report}
          closing={closing}
          onCancel={() => setPreview(null)}
          onClose={() => void close(preview.period)}
        />
      )}
    </div>
  );
}

/** Create a period. Enter accepts, Esc cancels. */
function PeriodDialog({
  onCancel,
  onDone,
}: {
  onCancel: () => void;
  onDone: (period: PeriodOut) => void;
}) {
  const [name, setName] = useState('');
  const [startsOn, setStartsOn] = useState('');
  const [endsOn, setEndsOn] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useFocusScope('periodDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = useCallback(async () => {
    if (!name.trim() || !startsOn || !endsOn) {
      setError('Name, from and to dates are all required (YYYY-MM-DD)');
      return;
    }
    setBusy(true);
    setError('');
    try {
      onDone(await api.createPeriod({ name: name.trim(), starts_on: startsOn, ends_on: endsOn }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The engine refused the period');
    } finally {
      setBusy(false);
    }
  }, [name, startsOn, endsOn, onDone]);

  useCommand(
    { id: 'dialog.period.accept', title: 'Create the period', run: () => void submit() },
    'Enter',
    (ctx) => ctx.scopeId === 'periodDialog',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card" role="dialog" aria-label="Create a period">
        <h2>Create a period</h2>
        <label className="dialog-field">
          Name
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setError('');
            }}
            aria-label="Period name"
            placeholder="FY 2026-27"
            aria-invalid={error !== ''}
          />
        </label>
        <div className="dialog-fields-row">
          <label className="dialog-field">
            From
            <input
              type="date"
              value={startsOn}
              onChange={(e) => setStartsOn(e.target.value)}
              aria-label="Period start (YYYY-MM-DD)"
            />
          </label>
          <label className="dialog-field">
            To
            <input
              type="date"
              value={endsOn}
              onChange={(e) => setEndsOn(e.target.value)}
              aria-label="Period end (YYYY-MM-DD)"
            />
          </label>
        </div>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <p className="overlay-foot">
          Enter create · Esc cancel — ranges must not overlap an existing period
        </p>
        <div className="overlay-actions">
          <button type="button" onClick={() => void submit()} disabled={busy}>
            {busy ? 'Working…' : 'Create'}
          </button>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/** The close preview: exactly what closing would post. Enter closes. */
function ClosePreviewDialog({
  period,
  report,
  closing,
  onCancel,
  onClose,
}: {
  period: PeriodOut;
  report: FyCloseReportOut;
  closing: boolean;
  onCancel: () => void;
  onClose: () => void;
}) {
  useFocusScope('closePreview', () => {
    onCancel();
    return true;
  });

  useCommand(
    {
      id: 'dialog.closePreview.confirm',
      title: `Close ${period.name} — post the FY sweep`,
      run: onClose,
    },
    'Enter',
    (ctx) => ctx.scopeId === 'closePreview',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card close-preview" role="dialog" aria-label={`Close ${period.name}`}>
        <h2>Close {period.name}?</h2>
        <p className="payslip-note">
          Exactly what closing posts, as of {report.as_of} — every income and expense
          ledger zeroed, the balance swept into Profit &amp; Loss A/c. The sweep is an FYC
          voucher dated the next period&rsquo;s first day; the period is then{' '}
          <strong>closed, final</strong> (reversal vouchers above it still work).
        </p>
        <table className="daybook-entries" aria-label="The close sweep">
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
            {report.lines.map((line, i) => (
              <tr key={`${line.ledger_id}-${line.dc}-${i}`}>
                <td>{i + 1}</td>
                <td>{line.ledger_name}</td>
                <td>{line.dc === 'D' ? 'Dr' : 'Cr'}</td>
                <td className="num mono">{money(line.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="payslip-note">
          <strong>Net profit: {money(report.net_profit)}</strong> — debits equal credits to
          the cent, always.
        </p>
        <div className="overlay-actions">
          <button type="button" autoFocus onClick={onClose} disabled={closing}>
            {closing ? 'Closing…' : 'Close the period'}
          </button>
          <button type="button" onClick={onCancel}>
            Keep it open
          </button>
        </div>
        <p className="overlay-foot">Enter close · Esc keep it open</p>
      </div>
    </div>
  );
}
