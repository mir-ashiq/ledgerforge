/**
 * The banking screen (ADR-026, Phase 5): statement import with preview,
 * the line book with the matchmaker's suggestions, match/un-match, the
 * reconciliation proof, and the cheque register.
 *
 * `Alt+G` opens it (Alt+B has belonged to the daybook since ADR-015).
 * `↑/↓` walk the lines, `M` matches the selected line
 * to the highlighted (or best) suggestion — the ONLY commit the flow
 * ever makes (suggestions never auto-commit) — `U` un-matches, `R`
 * refreshes, `Tab` switches the pane between lines / proof / cheques.
 * Every number is the engine's string — the client never sums (Law 3);
 * the proof row is the engine's own exact verdict.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type {
  BankChequeRegister,
  BankImportReceipt,
  BankLedgerOut,
  BankLine,
  BankReconciliation,
  BankSuggestions,
} from '../api/types';
import { Banner, type Notice } from './Banner';

type LoadState = 'loading' | 'ready' | 'error';
type Pane = 'lines' | 'proof' | 'cheques';

interface BankingScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

export function BankingScreen({ onExit, ctxRef }: BankingScreenProps) {
  const [banks, setBanks] = useState<BankLedgerOut[]>([]);
  const [bankId, setBankId] = useState<number | null>(null);
  const [pane, setPane] = useState<Pane>('lines');
  const [lines, setLines] = useState<BankLine[]>([]);
  const [suggestions, setSuggestions] = useState<BankSuggestions | null>(null);
  const [unmatchedOnly, setUnmatchedOnly] = useState(false);
  const [index, setIndex] = useState(0);
  const [suggIndex, setSuggIndex] = useState(0);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [receipt, setReceipt] = useState<BankImportReceipt | null>(null);
  const [proof, setProof] = useState<BankReconciliation | null>(null);
  const [cheques, setCheques] = useState<BankChequeRegister | null>(null);
  const [statementBalance, setStatementBalance] = useState('');
  const [asOf, setAsOf] = useState(new Date().toISOString().slice(0, 10));
  const importRef = useRef<HTMLTextAreaElement | null>(null);

  useFocusScope('bankingScreen', () => {
    onExit();
    return true;
  });
  ctxRef.current = { bankingOpen: true, importOpen };
  useEffect(() => () => {
    ctxRef.current = {};
  }, [ctxRef]);

  // ---- the picker feed, once -------------------------------------------
  useEffect(() => {
    let cancelled = false;
    api
      .bankLedgers()
      .then((rows) => {
        if (cancelled) return;
        setBanks(rows);
        if (rows.length) setBankId((current) => current ?? rows[0].id);
        else setLoadState('error');
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not read bank accounts' });
        setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- the line book + the matchmaker, on every bank/status change -----
  useEffect(() => {
    if (bankId === null) return;
    let cancelled = false;
    setLoadState('loading');
    Promise.all([
      api.bankLines(bankId, unmatchedOnly ? 'unmatched' : 'all'),
      api.bankSuggestions(bankId).catch(() => ({ ledger_id: bankId, window_days: 7, lines: [] })),
    ])
      .then(([book, sugg]) => {
        if (cancelled) return;
        setLines(book.lines);
        setSuggestions(sugg);
        setLoadState('ready');
        setIndex(0);
        setSuggIndex(0);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not read the statement book' });
        setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [bankId, unmatchedOnly, notice?.kind === 'err' ? 0 : 0]);

  const selected = lines[Math.min(index, Math.max(0, lines.length - 1))] ?? null;
  const selectedSuggestions = useMemo(() => {
    if (!selected || !suggestions) return [];
    const entry = suggestions.lines.find((s) => s.statement_line_id === selected.id);
    return entry?.suggestions ?? [];
  }, [selected, suggestions]);

  useEffect(() => {
    document
      .querySelector(`[data-bank-line="${index}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [index]);

  const inList = () => ctxRef.current.bankingOpen === true && !importOpen;

  useCommand(
    { id: 'banking.up', title: 'Banking: previous line', run: () => setIndex((i) => Math.max(0, i - 1)) },
    'ArrowUp',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.down', title: 'Banking: next line', run: () => setIndex((i) => Math.min(lines.length - 1, i + 1)) },
    'ArrowDown',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.nextSuggestion', title: 'Banking: next suggestion', run: () => setSuggIndex((i) => i + 1) },
    'ArrowRight',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.prevSuggestion', title: 'Banking: previous suggestion', run: () => setSuggIndex((i) => Math.max(0, i - 1)) },
    'ArrowLeft',
    inList as () => boolean,
  );
  useCommand(
    {
      id: 'banking.match',
      title: 'Banking: match line to the highlighted suggestion',
      run: () => void doMatch(),
    },
    'M',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.unmatch', title: 'Banking: withdraw the selected match', run: () => void doUnmatch() },
    'U',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.refresh', title: 'Banking: refresh', run: () => setNotice(null) },
    'R',
    inList as () => boolean,
  );
  useCommand(
    { id: 'banking.pane', title: 'Banking: switch pane (lines / proof / cheques)', run: () => setPane((p) => (p === 'lines' ? 'proof' : p === 'proof' ? 'cheques' : 'lines')) },
    'Tab',
    inList as () => boolean,
  );

  // ---- the actions ------------------------------------------------------
  async function doMatch() {
    if (bankId === null || !selected || selectedSuggestions.length === 0) return;
    if (selected.match?.live) {
      setNotice({ kind: 'err', text: 'This line is already matched — un-match it first (U).' });
      return;
    }
    const candidate =
      selectedSuggestions[Math.min(suggIndex, selectedSuggestions.length - 1)] ??
      selectedSuggestions[0];
    try {
      await api.bankMatch(bankId, {
        statement_line_id: selected.id,
        entry_id: candidate.entry_id,
        voucher_id: candidate.voucher_id,
        voucher_date: candidate.voucher_date,
      });
      setNotice({ kind: 'ok', text: `Matched to ${candidate.voucher_no}.` });
      refreshLines();
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The match was refused' });
    }
  }

  async function doUnmatch() {
    if (!selected?.match?.live) {
      setNotice({ kind: 'err', text: 'This line has no live match to withdraw.' });
      return;
    }
    try {
      await api.bankUnmatch(selected.id);
      setNotice({ kind: 'ok', text: 'Match withdrawn — the row keeps its provenance.' });
      refreshLines();
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The un-match was refused' });
    }
  }

  function refreshLines() {
    setNotice((n) => (n?.kind === 'ok' ? n : null));
    // retrigger the load effect without a full remount
    setLoadState('loading');
    if (bankId !== null) {
      Promise.all([
        api.bankLines(bankId, unmatchedOnly ? 'unmatched' : 'all'),
        api.bankSuggestions(bankId).catch(() => ({ ledger_id: bankId, window_days: 7, lines: [] })),
      ]).then(([book, sugg]) => {
        setLines(book.lines);
        setSuggestions(sugg);
        setLoadState('ready');
      }).catch((e: unknown) => {
        setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'Could not read the statement book' });
        setLoadState('error');
      });
    }
  }

  async function doImport(dryRun: boolean) {
    if (bankId === null) return;
    const text = importRef.current?.value ?? '';
    const format = text.includes(':61:') ? 'mt940' : 'csv';
    try {
      const rec = await api.bankImport(bankId, { format, text, dry_run: dryRun });
      setReceipt(rec);
      if (!dryRun) {
        setImportOpen(false);
        refreshLines();
      }
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The statement was refused' });
    }
  }

  async function runProof() {
    if (bankId === null) return;
    try {
      const data = await api.bankReconciliation(
        bankId,
        asOf,
        statementBalance.trim() || undefined,
      );
      setProof(data);
      setPane('proof');
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The proof was refused' });
    }
  }

  async function runCheques() {
    try {
      const bank = banks.find((b) => b.id === bankId);
      const data = await api.bankCheques(asOf, bank?.name);
      setCheques(data);
      setPane('cheques');
    } catch (e) {
      setNotice({ kind: 'err', text: e instanceof Error ? e.message : 'The register was refused' });
    }
  }

  const bankName = banks.find((b) => b.id === bankId)?.name ?? '';

  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Banking{bankName ? ` — ${bankName}` : ''}</span>
        <select
          aria-label="Bank account"
          value={bankId ?? ''}
          onChange={(e) => setBankId(Number(e.target.value))}
        >
          {banks.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name} ({b.balance})
            </option>
          ))}
        </select>
        <button type="button" onClick={() => setUnmatchedOnly((v) => !v)}>
          {unmatchedOnly ? 'Unmatched only' : 'All lines'}
        </button>
        <button type="button" onClick={() => setImportOpen(true)}>
          Import…
        </button>
        <span className="spacer" />
        <label className="hint" htmlFor="banking-as-of">
          As of
        </label>
        <input
          id="banking-as-of"
          type="date"
          value={asOf}
          onChange={(e) => setAsOf(e.target.value)}
        />
        <input
          className="filter-input"
          aria-label="Balance per statement (blank = fold)"
          placeholder="Balance per statement (blank = fold)"
          value={statementBalance}
          onChange={(e) => setStatementBalance(e.target.value)}
        />
        <button type="button" onClick={() => void runProof()}>
          Reconcile
        </button>
        <button type="button" onClick={() => void runCheques()}>
          Cheques
        </button>
      </header>

      <Banner notice={notice} loading={loadState === 'loading'} apiError={false} onRetry={refreshLines} />

      <main className="banking-area">
        {pane === 'lines' && (
          <>
            <table className="outstanding-table" aria-label="Statement lines">
              <thead>
                <tr>
                  <th>Date</th>
                  <th className="num">Withdrawal</th>
                  <th className="num">Deposit</th>
                  <th>Narrative</th>
                  <th>Reference</th>
                  <th>Matched to</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line, i) => (
                  <tr key={line.id} data-bank-line={i} className={i === index ? 'sel' : ''}>
                    <td>{line.value_date}</td>
                    <td className="num">{line.dc === 'D' ? line.amount : ''}</td>
                    <td className="num">{line.dc === 'C' ? line.amount : ''}</td>
                    <td>{line.narrative ?? ''}</td>
                    <td>{line.reference ?? ''}</td>
                    <td>
                      {line.match?.live
                        ? `${line.match.voucher_no} (${line.match.voucher_date})`
                        : line.match
                          ? 'withdrawn'
                          : '—'}
                    </td>
                  </tr>
                ))}
                {!lines.length && (
                  <tr>
                    <td colSpan={6} className="empty-note">
                      No statement lines{unmatchedOnly ? ' unmatched' : ''} — import a
                      statement to begin.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            <section className="banking-suggestions" aria-label="Suggestions">
              <h3 className="hint">
                Suggestions for the selected line {selected ? `(${selected.value_date})` : ''}
                — M matches the highlighted one; suggestions never commit themselves
              </h3>
              <ul>
                {selectedSuggestions.map((c, i) => (
                  <li key={c.entry_id} className={i === Math.min(suggIndex, selectedSuggestions.length - 1) ? 'sel' : ''}>
                    <span className="num">{c.score}</span>{' '}
                    <strong>{c.voucher_no}</strong> {c.voucher_date}{' '}
                    <span className="num">{c.amount}</span> {c.narration ?? ''}{' '}
                    <em>{c.reasons.join(', ')}</em>
                  </li>
                ))}
                {!selectedSuggestions.length && (
                  <li className="empty-note">No candidates — match manually after posting the voucher.</li>
                )}
              </ul>
            </section>
          </>
        )}

        {pane === 'proof' && proof && (
          <section aria-label="Bank reconciliation statement" className="banking-proof">
            <table className="outstanding-table">
              <thead>
                <tr>
                  <th>Particulars</th>
                  <th>Date</th>
                  <th>Reference</th>
                  <th className="num">Effect</th>
                </tr>
              </thead>
              <tbody>
                {proof.rows.map((row, i) => (
                  <tr key={i}>
                    <td>{row.label}</td>
                    <td>{row.date}</td>
                    <td>{row.reference ?? ''}</td>
                    <td className="num">{row.effect}</td>
                  </tr>
                ))}
                {!proof.rows.length && (
                  <tr>
                    <td colSpan={4} className="empty-note">Fully reconciled.</td>
                  </tr>
                )}
              </tbody>
            </table>
            <p className="hint">
              Books {proof.book_balance} · Statement {proof.statement_balance} (
              {proof.statement_balance_source}) · Reconciled{' '}
              {proof.reconciled_balance} · Proof {proof.proof.lhs} = {proof.proof.rhs} —
              exact (ADR-026).
            </p>
          </section>
        )}

        {pane === 'cheques' && cheques && (
          <section aria-label="Cheque register" className="banking-cheques">
            <table className="outstanding-table">
              <thead>
                <tr>
                  <th>Bank</th>
                  <th>Cheque No</th>
                  <th>Voucher</th>
                  <th>Issued</th>
                  <th>Matures</th>
                  <th className="num">Amount</th>
                  <th>Status</th>
                  <th>Presented</th>
                  <th>Cleared by</th>
                </tr>
              </thead>
              <tbody>
                {cheques.rows.map((row) => (
                  <tr key={`${row.bank_ledger}:${row.no}`}>
                    <td>{row.bank_ledger}</td>
                    <td>{row.no}</td>
                    <td>{row.voucher_no}</td>
                    <td>{row.issued_on}</td>
                    <td>{row.matures_on ?? '—'}</td>
                    <td className="num">{row.amount ?? '—'}</td>
                    <td>{row.status}{row.pdc ? ' (PDC)' : ''}</td>
                    <td>{row.presented_on ?? '—'}</td>
                    <td>{row.cleared_by ?? '—'}</td>
                  </tr>
                ))}
                {!cheques.rows.length && (
                  <tr>
                    <td colSpan={9} className="empty-note">No cheques declared.</td>
                  </tr>
                )}
              </tbody>
            </table>
            <p className="hint">
              issued → presented (a statement line names the number) → cleared (that
              line is matched). PDC matures after its issue date (ADR-026).
            </p>
          </section>
        )}
      </main>

      {importOpen && (
        <div className="modal" role="dialog" aria-label="Import statement">
          <div className="modal-card">
            <h3>Import statement — paste the text (CSV or MT940)</h3>
            <textarea
              ref={importRef}
              rows={10}
              placeholder={
                'Date,Narrative,Ref No,Withdrawal,Deposit\n01/04/2026,…  — or —\n:20:STMNT1\n:61:260403D2500,00NTRF…'
              }
            />
            {receipt?.dry_run && (
              <p className="hint">
                Preview: {receipt.parsed} parsed, {receipt.new} would import,{' '}
                {receipt.duplicates} duplicates, {receipt.informational} informational rows.
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
        Banking — {lines.length} lines loaded
      </div>
    </div>
  );
}
