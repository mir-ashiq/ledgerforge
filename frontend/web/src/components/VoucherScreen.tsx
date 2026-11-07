/**
 * The voucher-entry screen — LedgerForge's first real screen (issue #2).
 *
 * Everything is reachable from the keyboard: Enter advances cell-by-cell and
 * auto-appends rows, the ledger picker is type-ahead over the prefetched
 * index, Alt+C creates a missing ledger mid-flight, F2 changes the date,
 * F4–F9 switch the voucher type, Ctrl+Enter fills the balancing amount and
 * Ctrl+A accepts — an idempotent POST with amounts as strings (Law 3).
 * Ctrl+P prints the last posted voucher through the same endpoint the
 * payslip screen uses (ADR-013): any posting voucher — sale, journal,
 * payment — prints as its document.
 */

import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { ContextPredicate } from '@ledgerforge/keyboard-kit';
import { api, type CreateLedgerInput } from '../api/client';
import type { GroupOut, LedgerOut, VoucherOut } from '../api/types';
import { isZero, toCanonical } from '../money/exact';
import {
  DEFAULT_TYPE_CODE,
  VOUCHER_TYPES,
  type CellCol,
  type Cursor,
  initDraft,
} from '../voucher/model';
import { voucherReducer } from '../voucher/reducer';
import { rankEntries } from '../voucher/pickerFilter';
import { computeTotals, isPristine, validateForPost } from '../voucher/validate';
import { BalanceHud } from './BalanceHud';
import { Banner, type Notice } from './Banner';
import { ConfirmOverlay } from './ConfirmOverlay';
import { DateDialog } from './DateDialog';
import { LedgerCreateDialog } from './LedgerCreateDialog';
import { VoucherGrid } from './VoucherGrid';

type ApiState = 'loading' | 'ready' | 'error';
type Dialog = 'date' | 'ledgerCreate' | 'confirmNew' | null;

interface VoucherScreenProps {
  ledgers: readonly LedgerOut[];
  groups: readonly GroupOut[];
  apiState: ApiState;
  /** The active book's name (ADR-009 in the UI) — shown in the bar so the
   *  accountant always knows which company's books are open; null when the
   *  server auto-resolves the single active book. */
  bookName?: string | null;
  onRetryMasters: () => void;
  onLedgerCreated: (ledger: LedgerOut) => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const inEditor: ContextPredicate = (ctx) => ctx.scopeId === 'voucherEditor';
const editorAnywhere: ContextPredicate = (ctx) => Array.isArray(ctx.scopeIds) && ctx.scopeIds.includes('voucherEditor');
const inLedgerPicker: ContextPredicate = (ctx) => ctx.scopeId === 'ledgerPicker';
/** The command palette sits on top of this screen: while it is open, this
 *  screen's keys are inert (only palette keys respond) — same contract as a
 *  TallyPrime menu. */
const paletteClosed: ContextPredicate = (ctx) => ctx.paletteOpen !== true;
/** Voucher-scoped, with the picker open too, but not while a dialog is up. */
const noDialog: ContextPredicate = (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx);

export function VoucherScreen({
  ledgers,
  groups,
  apiState,
  bookName = null,
  onRetryMasters,
  onLedgerCreated,
  ctxRef,
}: VoucherScreenProps) {
  const [draft, dispatch] = useReducer(voucherReducer, undefined, () => initDraft(DEFAULT_TYPE_CODE));
  const [picker, setPicker] = useState<{ highlight: number } | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [ledgerCreate, setLedgerCreate] = useState<{ row: number; seed: string } | null>(null);
  const [posting, setPosting] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [announce, setAnnounce] = useState('');
  const [lastPosted, setLastPosted] = useState<VoucherOut | null>(null);
  const [printing, setPrinting] = useState(false);
  const postingRef = useRef(false);
  const printingRef = useRef(false);
  // read at keydown/run time — always fresh, never a stale closure
  const lastPostedRef = useRef<VoucherOut | null>(null);
  lastPostedRef.current = lastPosted;
  const printUrlRef = useRef<string | null>(null);

  // --- focus scope: this screen is the 'voucherEditor' -----------------------
  // Escape at the top of the stack is consumed for now — a gateway screen
  // will own the back-navigation later (docs/keyboard-ux.md §8).
  useFocusScope('voucherEditor', () => true);

  const cursor = draft.cursor;
  const activeLine = draft.lines[cursor.row];
  const filterText = cursor.col === 'ledger' ? (activeLine?.ledgerName ?? '') : '';
  const pickerEntries = useMemo(
    () => rankEntries(ledgers.map((l) => ({ id: l.id, name: l.name, context: l.group_name })), filterText),
    [ledgers, filterText],
  );
  const defaultGroupName =
    groups.find((g) => g.name === 'Suspense A/c' && !g.is_root)?.name ??
    groups.find((g) => !g.is_root)?.name ??
    '';

  // --- focus management ------------------------------------------------------
  function focusCell(target: Cursor) {
    const el = document.querySelector<HTMLElement>(`[data-cell="${target.row}:${target.col}"]`);
    el?.focus();
    el?.scrollIntoView?.({ block: 'nearest' });
  }

  useEffect(() => {
    focusCell(draft.cursor);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.cursor]);

  // the picker follows the ledger cell; it closes when focus leaves those cells
  useEffect(() => {
    if (draft.cursor.col !== 'ledger') setPicker(null);
  }, [draft.cursor.col]);

  useEffect(() => {
    setPicker((p) => (p ? { ...p, highlight: 0 } : p));
  }, [filterText]);

  // live flags for keymap predicates (read at keydown time, always fresh)
  ctxRef.current = {
    inLedgerCell: cursor.col === 'ledger',
    inAmountCell: cursor.col === 'debit' || cursor.col === 'credit',
    amountCellEmpty:
      (cursor.col === 'debit' && activeLine?.debit === '') ||
      (cursor.col === 'credit' && activeLine?.credit === ''),
    pickerOpen: picker !== null,
    dialogOpen: dialog !== null,
    posting,
  };

  // the print flow's blob URL is revoked when this screen unmounts
  useEffect(
    () => () => {
      if (printUrlRef.current) URL.revokeObjectURL?.(printUrlRef.current);
    },
    [],
  );

  function onFocusCell(row: number, col: CellCol) {
    if (row !== cursor.row || col !== cursor.col) dispatch({ type: 'cursor', row, col });
    if (col === 'ledger') setPicker((p) => p ?? { highlight: 0 });
  }

  function onCellChange(row: number, col: CellCol, value: string) {
    dispatch({ type: 'setCell', row, col, value });
    if (col === 'ledger') setPicker((p) => p ?? { highlight: 0 });
  }

  // --- commands ---------------------------------------------------------------
  useCommand(
    {
      id: 'focus.forward',
      title: 'Next cell (Enter)',
      run: () => dispatch({ type: 'cellForward', row: cursor.row, col: cursor.col }),
    },
    'Enter',
    inEditor,
  );

  useCommand(
    {
      id: 'focus.backward',
      title: 'Previous cell',
      run: () => dispatch({ type: 'cellBack', row: cursor.row, col: cursor.col }),
    },
    'Shift+Enter',
    inEditor,
  );

  useCommand(
    {
      id: 'voucher.accept',
      title: 'Accept voucher',
      run: () => void acceptVoucher(),
    },
    'Primary+A',
    // fires from anywhere in the voucher — with the picker open too — but
    // never while a dialog or the command palette owns the interaction
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx),
  );

  useCommand(
    {
      id: 'voucher.deleteLine',
      title: 'Delete line',
      run: () => {
        dispatch({ type: 'deleteLine', row: cursor.row });
        setPicker(null);
      },
    },
    'Alt+D',
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx),
  );

  useCommand(
    {
      id: 'voucher.changeDate',
      title: 'Change voucher date',
      run: () => setDialog('date'),
    },
    'F2',
    noDialog,
  );

  for (const t of VOUCHER_TYPES) {
    // eslint-disable-next-line react-hooks/rules-of-hooks -- static list, stable order
    useCommand(
      {
        id: `voucher.setType.${t.name.toLowerCase()}`,
        title: `Voucher type: ${t.name}`,
        run: () => dispatch({ type: 'setType', code: t.code }),
      },
      t.key,
      noDialog,
    );
  }

  useCommand(
    {
      id: 'voucher.new',
      title: 'New voucher',
      run: () => {
        if (isPristine(draft.lines)) {
          dispatch({ type: 'newDraft' });
          setNotice(null);
        } else {
          setDialog('confirmNew');
        }
      },
    },
    'Alt+N',
    noDialog,
  );

  useCommand(
    {
      id: 'voucher.fillBalance',
      title: 'Fill balancing amount',
      run: () => {
        const totals = computeTotals(draft.lines);
        if (isZero(totals.difference)) {
          setNotice({ kind: 'info', text: 'Already balanced — nothing to fill' });
          return;
        }
        const value = totals.difference.neg
          ? { neg: false, int: totals.difference.int, scale: totals.difference.scale }
          : totals.difference;
        dispatch({ type: 'fillBalance', row: cursor.row, col: cursor.col as 'debit' | 'credit', amount: toCanonical(value) });
      },
    },
    'Ctrl+Enter',
    (ctx) =>
      editorAnywhere(ctx) &&
      ctx.dialogOpen !== true &&
      paletteClosed(ctx) &&
      ctx.inAmountCell === true &&
      ctx.amountCellEmpty === true,
  );

  useCommand(
    {
      id: 'ledger.create.here',
      title: 'Create ledger here (Alt+C)',
      run: () => {
        const line = draft.lines[cursor.row];
        const seed = line && line.ledgerId === null ? line.ledgerName.trim() : '';
        setLedgerCreate({ row: cursor.row, seed });
        setDialog('ledgerCreate');
      },
    },
    'Alt+C',
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx) && ctx.inLedgerCell === true,
  );

  useCommand(
    {
      id: 'voucher.print',
      title: 'Print last posted voucher',
      run: () => {
        if (!lastPostedRef.current) {
          const hint = 'Nothing posted yet — accept a voucher first (Ctrl+A), then Ctrl+P prints it';
          setNotice({ kind: 'info', text: hint });
          setAnnounce(hint);
          return;
        }
        void printLastPosted();
      },
    },
    'Primary+P',
    // same firing window as accept: anywhere in the editor, picker included,
    // but never while a dialog or the palette owns the interaction
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx),
  );

  useCommand(
    {
      id: 'selection.moveUp',
      title: 'Picker: previous match',
      run: () =>
        setPicker((p) =>
          p && pickerEntries.length ? { ...p, highlight: (p.highlight - 1 + pickerEntries.length) % pickerEntries.length } : p,
        ),
    },
    'ArrowUp',
    inLedgerPicker,
  );

  useCommand(
    {
      id: 'selection.moveDown',
      title: 'Picker: next match',
      run: () =>
        setPicker((p) =>
          p && pickerEntries.length ? { ...p, highlight: (p.highlight + 1) % pickerEntries.length } : p,
        ),
    },
    'ArrowDown',
    inLedgerPicker,
  );

  useCommand(
    {
      id: 'selection.accept',
      title: 'Picker: accept highlighted ledger',
      run: () => pickLedger(picker?.highlight ?? 0),
    },
    'Enter',
    inLedgerPicker,
  );

  // --- flows --------------------------------------------------------------------
  function pickLedger(index: number) {
    const entry = pickerEntries[index];
    if (!entry || cursor.col !== 'ledger') return;
    dispatch({ type: 'setLedger', row: cursor.row, ledgerId: entry.id, ledgerName: entry.name });
    dispatch({ type: 'cellForward', row: cursor.row, col: 'ledger' });
    setPicker(null);
  }

  async function acceptVoucher() {
    if (postingRef.current) return;
    const check = validateForPost(draft);
    if (!check.ok) {
      setNotice({ kind: 'err', text: check.error });
      setAnnounce(check.error);
      return;
    }
    postingRef.current = true;
    setPosting(true);
    setNotice({ kind: 'info', text: 'Posting…' });
    try {
      const out = await api.postVoucher({
        voucher_type: draft.typeCode,
        voucher_date: draft.date,
        client_token: draft.clientToken,
        entries: check.lines,
      });
      const message = out.replayed
        ? `Voucher ${out.voucher_no} was already posted — idempotent replay, nothing duplicated`
        : `Voucher ${out.voucher_no} posted`;
      setNotice({ kind: 'ok', text: message });
      setAnnounce(message);
      setLastPosted(out);
      dispatch({ type: 'newDraft' }); // same type & date: chain entries at speed
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Posting failed';
      setNotice({ kind: 'err', text: `${message} — the draft is intact; Ctrl+A retries safely` });
      setAnnounce(message);
    } finally {
      postingRef.current = false;
      setPosting(false);
    }
  }

  async function printLastPosted() {
    const posted = lastPostedRef.current;
    if (!posted || printingRef.current) return;
    printingRef.current = true;
    setPrinting(true);
    try {
      const blob = await api.voucherPdf(posted.id);
      if (printUrlRef.current) URL.revokeObjectURL?.(printUrlRef.current);
      const url = URL.createObjectURL(blob);
      printUrlRef.current = url;
      const opened = window.open(url, '_blank', 'noopener');
      if (!opened) {
        throw new Error('The browser blocked the PDF tab — allow pop-ups for this site and retry');
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Could not fetch the PDF';
      setNotice({ kind: 'err', text: message });
      setAnnounce(message);
    } finally {
      printingRef.current = false;
      setPrinting(false);
    }
  }

  async function createLedgerHere(input: CreateLedgerInput): Promise<void> {
    const target = ledgerCreate;
    const created = await api.createLedger(input);
    onLedgerCreated(created);
    setDialog(null);
    setLedgerCreate(null);
    if (target && draft.lines[target.row]) {
      dispatch({ type: 'setLedger', row: target.row, ledgerId: created.id, ledgerName: created.name });
      dispatch({ type: 'cellForward', row: target.row, col: 'ledger' });
    }
    const message = `Ledger "${created.name}" created under ${created.group_name}`;
    setNotice({ kind: 'ok', text: message });
    setAnnounce(message);
  }

  function closeDialog() {
    setDialog(null);
    setLedgerCreate(null);
    // Refocus the grid cell synchronously — a deferred (rAF) callback could
    // fire after a *newly opened* dialog has taken focus and steal it back.
    focusCell(draft.cursor);
  }

  // --- render --------------------------------------------------------------------
  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">
          Voucher Entry{bookName ? ` — ${bookName}` : ''}
        </span>
        <div className="types" role="tablist" aria-label="Voucher type">
          {VOUCHER_TYPES.map((t) => (
            <button
              key={t.code}
              type="button"
              role="tab"
              aria-selected={t.code === draft.typeCode}
              className={t.code === draft.typeCode ? 'on' : ''}
              onClick={() => dispatch({ type: 'setType', code: t.code })}
            >
              {t.name} <kbd>{t.key}</kbd>
            </button>
          ))}
        </div>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={() => setDialog('date')}>
          {draft.date} <kbd>F2</kbd>
        </button>
        {lastPosted && (
          <>
            <span className="last-posted">Last posted: {lastPosted.voucher_no}</span>
            <button
              type="button"
              className="date-chip"
              onClick={() => void printLastPosted()}
              disabled={printing}
            >
              {printing ? 'Printing…' : `Print ${lastPosted.voucher_no}`} <kbd>Ctrl+P</kbd>
            </button>
          </>
        )}
      </header>

      <Banner
        notice={notice}
        loading={apiState === 'loading'}
        apiError={apiState === 'error'}
        onRetry={onRetryMasters}
      />

      <main className="grid-area">
        <VoucherGrid
          draft={draft}
          ledgers={ledgers}
          pickerOpen={picker !== null}
          pickerEntries={pickerEntries}
          pickerHighlight={picker?.highlight ?? 0}
          onPickerPick={pickLedger}
          onPickerClose={() => setPicker(null)}
          onCellChange={onCellChange}
          onFocusCell={onFocusCell}
        />
      </main>

      <BalanceHud lines={draft.lines} />

      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      {dialog === 'date' && (
        <DateDialog
          date={draft.date}
          onAccept={(date) => {
            dispatch({ type: 'setDate', date });
            closeDialog();
          }}
          onCancel={closeDialog}
        />
      )}
      {dialog === 'ledgerCreate' && ledgerCreate && (
        <LedgerCreateDialog
          seedName={ledgerCreate.seed}
          defaultGroupName={defaultGroupName}
          groups={groups}
          onSubmit={createLedgerHere}
          onCancel={closeDialog}
        />
      )}
      {dialog === 'confirmNew' && (
        <ConfirmOverlay
          message="Discard this draft?"
          confirmLabel="Discard"
          onConfirm={() => {
            dispatch({ type: 'newDraft' });
            setNotice(null);
            closeDialog();
          }}
          onCancel={closeDialog}
        />
      )}
    </div>
  );
}
