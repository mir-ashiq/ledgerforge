/**
 * The payslip screen (ADR-013): browse posted payroll runs, preview the
 * figures, print the PDF. `Alt+P` opens it, `Ctrl+P` prints (the shipped
 * `report.print` binding), `Esc` is back to voucher entry — the same
 * keyboard contract as every LedgerForge screen. Amounts are displayed
 * exactly as the engine computed them: strings, never client arithmetic
 * (Law 3).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { VoucherOut } from '../api/types';
import {
  employeesOfRuns,
  findRun,
  payrollRunOf,
  periodsOfEmployee,
  runComputedEmployee,
} from '../payslip/model';

type LoadState = 'loading' | 'error' | 'ready' | 'empty';

interface PayslipScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

function money(value: string | undefined): string {
  return value ?? '—';
}

export function PayslipScreen({ onExit, ctxRef }: PayslipScreenProps) {
  const [runs, setRuns] = useState<readonly VoucherOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [employee, setEmployee] = useState('');
  const [period, setPeriod] = useState('');
  const [printing, setPrinting] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('payslipScreen', () => {
    onExit();
    return true;
  });

  // live flags for keymap predicates (read at keydown time, always fresh)
  ctxRef.current = { payslipOpen: true };
  useEffect(
    () => () => {
      ctxRef.current = {};
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .vouchers()
      .then((all) => {
        if (cancelled) return;
        const payrollRuns = all.filter((v) => payrollRunOf(v) !== null);
        setRuns(payrollRuns);
        setLoadState(payrollRuns.length ? 'ready' : 'empty');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const employees = useMemo(() => employeesOfRuns(runs), [runs]);
  const periods = useMemo(
    () => (employee ? periodsOfEmployee(runs, employee) : []),
    [runs, employee],
  );
  const selectedRun = useMemo(
    () => (employee && period ? findRun(runs, employee, period) : null),
    [runs, employee, period],
  );
  const figures = useMemo(() => {
    const doc = selectedRun ? payrollRunOf(selectedRun) : null;
    return doc ? runComputedEmployee(doc, employee) : null;
  }, [selectedRun, employee]);

  // keep the selections valid as data settles
  useEffect(() => {
    if (employee && employees.includes(employee)) return;
    setEmployee(employees[0] ?? '');
  }, [employees, employee]);
  useEffect(() => {
    if (period && periods.includes(period)) return;
    setPeriod(periods[0] ?? '');
  }, [periods, period]);

  const print = useCallback(async () => {
    if (!selectedRun || !employee || printing) return;
    setPrinting(true);
    setNote(null);
    try {
      const blob = await api.voucherPdf(selectedRun.id, employee);
      if (objectUrlRef.current) URL.revokeObjectURL?.(objectUrlRef.current);
      const url = URL.createObjectURL(blob);
      objectUrlRef.current = url;
      const opened = window.open(url, '_blank', 'noopener');
      if (!opened) {
        throw new Error('The browser blocked the PDF tab — allow pop-ups for this site and retry');
      }
    } catch (err) {
      setNote(err instanceof Error ? err.message : 'Could not fetch the payslip');
    } finally {
      setPrinting(false);
    }
  }, [selectedRun, employee, printing]);

  // Ctrl+P / Cmd+P prints (the shipped `report.print` binding) — bound for
  // this screen's lifetime; the palette lists it while it exists
  useCommand(
    { id: 'report.print', title: 'Print payslip', run: () => void print() },
    'Primary+P',
  );

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Payslips — payroll runs &amp; prints</span>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={onExit}>
          Voucher entry <kbd>Esc</kbd>
        </button>
      </header>

      <main className="payslip-body">
        {loadState === 'loading' && (
          <p className="payslip-note" role="status">
            Loading payroll runs…
          </p>
        )}
        {loadState === 'error' && (
          <p className="payslip-note bad" role="alert">
            Cannot reach the engine — is it running? (make dev)
          </p>
        )}
        {loadState === 'empty' && (
          <p className="payslip-note" role="status">
            No payroll runs posted yet. Post one with <code>meta.payroll</code> (ADR-012) —
            it appears here the moment it is posted.
          </p>
        )}

        {loadState === 'ready' && (
          <>
            <div className="payslip-form">
              <label>
                Employee
                <select
                  value={employee}
                  onChange={(e) => setEmployee(e.target.value)}
                  aria-label="Employee"
                >
                  {employees.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Period
                <select
                  value={period}
                  onChange={(e) => setPeriod(e.target.value)}
                  aria-label="Payroll period"
                  disabled={!employee}
                >
                  {periods.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="payslip-print"
                onClick={() => void print()}
                disabled={!selectedRun || printing}
              >
                {printing ? 'Fetching…' : 'Print payslip'} <kbd>Ctrl+P</kbd>
              </button>
            </div>

            {note && (
              <p className="payslip-note bad" role="alert">
                {note}
              </p>
            )}

            {selectedRun && (
              <div className="payslip-card" aria-label="Payslip preview">
                <div className="payslip-card-head">
                  <strong>{employee}</strong>
                  <span>
                    {period} · {selectedRun.voucher_no}
                  </span>
                </div>
                {figures ? (
                  <dl className="payslip-figures">
                    <div>
                      <dt>Gross earnings</dt>
                      <dd>{money(figures.gross)}</dd>
                    </div>
                    <div>
                      <dt>Deductions (withheld)</dt>
                      <dd>{money(figures.withheld)}</dd>
                    </div>
                    <div>
                      <dt>Employer contributions</dt>
                      <dd>{money(figures.employer)}</dd>
                    </div>
                    <div className="net">
                      <dt>Net pay</dt>
                      <dd>{money(figures.net)}</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="payslip-note">
                    This run predates computed annotations — the PDF prints from the
                    posted components.
                  </p>
                )}
                <p className="payslip-note">
                  The PDF opens in a new tab — print or save it from there. Reprints are
                  identical: posted entries are immutable (Law 2).
                </p>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
