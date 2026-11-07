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
import type {
  BillRef,
  CostAllocationIn,
  CostCategoryOut,
  CostCentreOut,
  GroupOut,
  LedgerOut,
  VoucherOut,
} from '../api/types';
import { add, isZero, parseAmount, sub, toCanonical, ZERO, type Exact } from '../money/exact';
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
import { BillAllocationDialog } from './BillAllocationDialog';
import { ConfirmOverlay } from './ConfirmOverlay';
import { CostAllocationDialog, type AllocatableLine } from './CostAllocationDialog';
import { DateDialog } from './DateDialog';
import { LedgerCreateDialog } from './LedgerCreateDialog';
import { VoucherGrid } from './VoucherGrid';

type ApiState = 'loading' | 'ready' | 'error';
type Dialog = 'date' | 'ledgerCreate' | 'confirmNew' | 'billAlloc' | 'costAlloc' | null;

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
  /** The armed bill allocation (ADR-023): rides the next post as meta.bills. */
  const [billAlloc, setBillAlloc] = useState<{ party: string; refs: BillRef[] } | null>(null);
  /** The armed cost splits (ADR-025): parallel to the draft's complete
   * lines, riding each line's cost_allocations on the post. */
  const [costAlloc, setCostAlloc] = useState<{
    lines: AllocatableLine[];
    splits: CostAllocationIn[][];
  } | null>(null);
  /** The cost masters, fetched lazily when Alt+K first opens the dialog. */
  const [costMasters, setCostMasters] = useState<{
    centres: CostCentreOut[];
    categories: CostCategoryOut[];
  } | null>(null);
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

  // --- bill-wise allocation (ADR-023): the cursor line's party, when it is one
  const PARTY_GROUPS = ['Sundry Debtors', 'Sundry Creditors'];
  const activeLedger = activeLine?.ledgerId !== null
    ? ledgers.find((l) => l.id === activeLine?.ledgerId) ?? null
    : null;
  const activeParty =
    activeLedger && PARTY_GROUPS.includes(activeLedger.group_name) ? activeLedger : null;
  /** The party's signed net movement in the draft (Σ Dr − Σ Cr), exact — or
   * null while any party line is unparsable (nothing to allocate yet). */
  const partyNet = useMemo<Exact | null>(() => {
    if (!activeParty) return null;
    let net = ZERO;
    for (const line of draft.lines) {
      if (line.ledgerId !== activeParty.id) continue;
      const dr = line.debit.trim();
      const cr = line.credit.trim();
      if (dr && cr) return null;
      const raw = dr || cr;
      if (!raw) continue;
      const parsed = parseAmount(raw);
      if (!parsed.ok) return null;
      net = dr ? add(net, parsed.value) : sub(net, parsed.value);
    }
    return net;
  }, [activeParty, draft.lines]);

  /** The draft's complete lines as the allocation dialog sees them
   * (ADR-025): ledger + exactly one positive amount, in row order. */
  const allocatableLines = useMemo<AllocatableLine[]>(() => {
    const rows: AllocatableLine[] = [];
    for (const line of draft.lines) {
      const dr = line.debit.trim();
      const cr = line.credit.trim();
      if (line.ledgerId === null && line.ledgerName.trim() === '') continue;
      if (dr && cr) continue; // both sides filled: not postable as-is
      const raw = dr || cr;
      if (!raw) continue;
      const parsed = parseAmount(raw);
      if (!parsed.ok || parsed.value.int === 0n) continue;
      const ledger =
        line.ledgerId !== null ? ledgers.find((l) => l.id === line.ledgerId) : undefined;
      rows.push({
        ledgerName: ledger?.name ?? line.ledgerName.trim(),
        dc: dr ? 'D' : 'C',
        amount: parsed.value,
      });
    }
    return rows;
  }, [draft.lines, ledgers]);

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
    partyLine: activeParty !== null,
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

  // bill-wise allocation (ADR-023): Alt+L with the cursor on a party line —
  // the on-account / against-ref decision at entry time, TallyPrime-style
  useCommand(
    {
      id: 'voucher.allocateBills',
      title: 'Allocate to bills (Alt+L)',
      run: () => {
        if (!activeParty) {
          const hint = 'The cursor line is not a party ledger (Sundry Debtors / Creditors)';
          setNotice({ kind: 'info', text: hint });
          setAnnounce(hint);
          return;
        }
        if (!partyNet || isZero(partyNet)) {
          const hint = `Enter ${activeParty.name}'s debit or credit first — the allocation follows the movement`;
          setNotice({ kind: 'info', text: hint });
          setAnnounce(hint);
          return;
        }
        setDialog('billAlloc');
      },
    },
    'Alt+L',
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx),
  );

  // cost-centre splits (ADR-025): Alt+K opens the per-line split dialog —
  // the remainder is always visible, every line fully allocated or left
  // unallocated. The masters load once, lazily.
  useCommand(
    {
      id: 'voucher.allocateCost',
      title: 'Allocate to cost centres (Alt+K)',
      run: () => {
        if (!allocatableLines.length) {
          const hint = 'No complete line yet — fill a ledger and its amount first; the splits follow the lines';
          setNotice({ kind: 'info', text: hint });
          setAnnounce(hint);
          return;
        }
        if (!costMasters) {
          void (async () => {
            try {
              const [centres, categories] = await Promise.all([
                api.costCentres(),
                api.costCategories(),
              ]);
              setCostMasters({ centres, categories });
            } catch {
              const hint = 'Could not load the cost centres — is the engine reachable?';
              setNotice({ kind: 'err', text: hint });
              setAnnounce(hint);
              return;
            }
            setDialog('costAlloc');
          })();
          return;
        }
        setDialog('costAlloc');
      },
    },
    'Alt+K',
    (ctx) => editorAnywhere(ctx) && ctx.dialogOpen !== true && paletteClosed(ctx),
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
      // the armed cost splits (ADR-025) attach to the lines they were armed
      // against — same ledger name AND amount, in order; an edited line
      // honestly loses its stale arming (it stays unallocated)
      const entries = [...check.lines];
      if (costAlloc) {
        let cursor = 0;
        for (const line of allocatableLines) {
          const splits = costAlloc.splits[cursor] ?? [];
          cursor += 1;
          if (!splits.length) continue;
          const target = entries.find(
            (e) =>
              e.ledger_id ===
                (ledgers.find((l) => l.name === line.ledgerName)?.id ?? -1) &&
              e.dc === line.dc &&
              e.amount === toCanonical(line.amount),
          );
          if (target) target.cost_allocations = splits;
        }
      }
      const out = await api.postVoucher({
        voucher_type: draft.typeCode,
        voucher_date: draft.date,
        client_token: draft.clientToken,
        entries,
        meta: billAlloc ? { bills: billAlloc } : undefined,
      });
      const message = out.replayed
        ? `Voucher ${out.voucher_no} was already posted — idempotent replay, nothing duplicated`
        : `Voucher ${out.voucher_no} posted`;
      setNotice({ kind: 'ok', text: message });
      setAnnounce(message);
      setLastPosted(out);
      setBillAlloc(null); // the allocation rode its voucher; the next starts clean
      setCostAlloc(null); // same for the cost splits (ADR-025)
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
        {billAlloc && (
          <button
            type="button"
            className="date-chip"
            onClick={() => setDialog('billAlloc')}
            title="Edit the armed bill allocation"
          >
            Bills: {billAlloc.party} ({billAlloc.refs.length} ref{billAlloc.refs.length === 1 ? '' : 's'})
          </button>
        )}
        {costAlloc && (
          <button
            type="button"
            className="date-chip"
            onClick={() => setDialog('costAlloc')}
            title="Edit the armed cost-centre splits"
          >
            Cost: {costAlloc.splits.filter((s) => s.length > 0).length} of{' '}
            {costAlloc.lines.length} line
            {costAlloc.lines.length === 1 ? '' : 's'} <kbd>Alt+K</kbd>
          </button>
        )}
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
            setBillAlloc(null);
            setCostAlloc(null);
            setNotice(null);
            closeDialog();
          }}
          onCancel={closeDialog}
        />
      )}
      {dialog === 'costAlloc' && costMasters && allocatableLines.length > 0 && (
        <CostAllocationDialog
          lines={allocatableLines}
          centres={costMasters.centres}
          categories={costMasters.categories}
          onApply={(splits) => {
            setCostAlloc({ lines: allocatableLines, splits });
            closeDialog();
            const armed = splits.filter((s) => s.length > 0).length;
            const message =
              `Cost centres armed: ${armed} of ${allocatableLines.length} line(s) allocated — ` +
              'Ctrl+A posts the voucher with them';
            setNotice({ kind: 'ok', text: message });
            setAnnounce(message);
          }}
          onCancel={closeDialog}
        />
      )}
      {dialog === 'billAlloc' && activeParty && partyNet && (
        <BillAllocationDialog
          party={activeParty.name}
          partyNet={partyNet}
          onApply={(refs) => {
            setBillAlloc({ party: activeParty.name, refs });
            closeDialog();
            const message =
              `Bills armed for ${activeParty.name}: ` +
              `${refs.length} ref${refs.length === 1 ? '' : 's'} — Ctrl+A posts the voucher with them`;
            setNotice({ kind: 'ok', text: message });
            setAnnounce(message);
          }}
          onCancel={closeDialog}
        />
      )}
    </div>
  );
}
