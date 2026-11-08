/**
 * Application root: owns the keymap lifecycle (with user-binding persistence),
 * the prefetched masters (ledger + group indexes), the active book (ADR-009 —
 * remembered across launches, switchable from the Books screen), screen
 * switching and the command palette. Screens: VoucherScreen (entry),
 * PayslipScreen (ADR-013), DaybookScreen (ADR-015), PluginsScreen (ADR-016),
 * SlotsScreen (ADR-019), BooksScreen and PeriodsScreen (Phase 1),
 * BankingScreen (Phase 5 / ADR-026), InventoryScreen (Phase 6 / ADR-027),
 * GstScreen (Phase 7 / ADR-028).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { keymap, useCommand } from '@ledgerforge/keyboard-kit/react';
import { api, setActiveBook } from './api/client';
import type { BookOut, GroupOut, LedgerOut } from './api/types';
import { BankingScreen } from './components/BankingScreen';
import { InventoryScreen } from './components/InventoryScreen';
import { BooksScreen } from './components/BooksScreen';
import { CommandPalette } from './components/CommandPalette';
import { DaybookScreen } from './components/DaybookScreen';
import { GstScreen } from './components/GstScreen';
import { OutstandingsScreen } from './components/OutstandingsScreen';
import { PayslipScreen } from './components/PayslipScreen';
import { PeriodsScreen } from './components/PeriodsScreen';
import { PluginsScreen } from './components/PluginsScreen';
import { SlotsScreen } from './components/SlotsScreen';
import { VoucherScreen } from './components/VoucherScreen';
import { localStorageKeymapStore } from './keymapStore';

type Screen =
  | 'voucher'
  | 'payslip'
  | 'daybook'
  | 'plugins'
  | 'slots'
  | 'books'
  | 'periods'
  | 'outstandings'
  | 'banking'
  | 'inventory'
  | 'gst';

/** Where the remembered book id lives between launches. */
const ACTIVE_BOOK_KEY = 'lf.activeBook';

export default function App() {
  const [ledgers, setLedgers] = useState<LedgerOut[]>([]);
  const [groups, setGroups] = useState<GroupOut[]>([]);
  const [apiState, setApiState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [showPalette, setShowPalette] = useState(false);
  const [screen, setScreen] = useState<Screen>('voucher');
  const [activeBook, setActiveBookState] = useState<BookOut | null>(null);
  const ctxRef = useRef<Record<string, unknown>>({});
  const paletteCtxRef = useRef<Record<string, unknown>>({});

  const loadMasters = useCallback(async () => {
    setApiState('loading');
    try {
      const [ledgerList, groupList] = await Promise.all([api.ledgers(), api.groups()]);
      setLedgers(ledgerList);
      setGroups(groupList);
      setApiState('ready');
    } catch {
      setApiState('error');
    }
  }, []);

  /** Select the book every request acts on (ADR-009): remembered between
   * launches; null falls back to the server's single-active-book resolution. */
  const adoptBook = useCallback((book: BookOut | null) => {
    setActiveBook(book ? book.id : null);
    setActiveBookState(book);
    if (book) window.localStorage.setItem(ACTIVE_BOOK_KEY, String(book.id));
    else window.localStorage.removeItem(ACTIVE_BOOK_KEY);
  }, []);

  // boot: resolve the book, then prefetch the masters. A failed books fetch
  // is not fatal — the long-standing single-book mode keeps working without
  // an explicit selection.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.books();
        if (cancelled) return;
        const stored = Number(window.localStorage.getItem(ACTIVE_BOOK_KEY));
        const remembered = list.find((b) => b.id === stored && !b.is_archived);
        if (remembered) adoptBook(remembered);
        else if (list.length === 1 && !list[0].is_archived) adoptBook(list[0]);
      } catch {
        /* books unreachable — proceed unselected (auto-resolution) */
      }
      await loadMasters();
    })();
    return () => {
      cancelled = true;
    };
  }, [adoptBook, loadMasters]);

  /** Switching books from the Books screen: adopt, remember, reload masters. */
  const selectBook = useCallback(
    (book: BookOut) => {
      adoptBook(book);
      void loadMasters();
    },
    [adoptBook, loadMasters],
  );

  // the single capture-phase keydown pipeline (ADR-004); user rebindings
  // persist across reloads through the localStorage store (keyboard-ux §8.3)
  useEffect(() => {
    keymap.setContextProvider(() => ({ ...ctxRef.current, ...paletteCtxRef.current }));
    void keymap.attachPersistence(localStorageKeymapStore);
    keymap.start();
    return () => keymap.stop();
  }, []);

  // global commands
  useCommand(
    { id: 'focus.back', title: 'Back / close (Esc)', run: () => { keymap.focus.escape(); } },
    'Escape',
  );
  useCommand(
    { id: 'palette.toggle', title: 'Command palette', run: () => setShowPalette((v) => !v) },
    'Primary+K',
  );
  // the payslip screen (ADR-013): Alt+P opens, Esc is back
  useCommand(
    { id: 'payslip.open', title: 'Payslips — view & print salary slips', run: () => setScreen('payslip') },
    'Alt+P',
  );
  // the daybook browser (ADR-015): Alt+B opens, Esc is back
  useCommand(
    { id: 'daybook.open', title: 'Daybook — browse & reprint posted vouchers', run: () => setScreen('daybook') },
    'Alt+B',
  );
  // the plugins screen (ADR-016): Alt+X (extensions) opens, Esc is back
  useCommand(
    { id: 'plugins.open', title: 'Plugins — loaded packs & trust status', run: () => setScreen('plugins') },
    'Alt+X',
  );
  // the slots screen (ADR-019): Alt+U opens, Esc is back
  useCommand(
    { id: 'slots.open', title: 'Slots — sandboxed plugin dashboards', run: () => setScreen('slots') },
    'Alt+U',
  );
  // the books screen (Phase 1 / ADR-009 in the UI): Alt+O opens, Esc is back
  useCommand(
    {
      id: 'books.open',
      title: 'Books — companies: select, create, archive',
      run: () => setScreen('books'),
    },
    'Alt+O',
  );
  // the periods screen (Phase 1 / ADR-008 in the UI): Alt+E opens, Esc is back
  useCommand(
    {
      id: 'periods.open',
      title: 'Periods — lock & the financial-year close',
      run: () => setScreen('periods'),
    },
    'Alt+E',
  );
  // the outstandings screen (Phase 2 / ADR-023): Alt+I opens, Esc is back
  useCommand(
    {
      id: 'outstandings.open',
      title: 'Outstanding — bills receivable/payable with ageing',
      run: () => setScreen('outstandings'),
    },
    'Alt+I',
  );
  // the banking screen (Phase 5 / ADR-026): Alt+G opens (gold — Alt+B
  // has belonged to the daybook since ADR-015), Esc is back
  useCommand(
    {
      id: 'banking.open',
      title: 'Banking — statements, reconciliation & cheques',
      run: () => setScreen('banking'),
    },
    'Alt+G',
  );
  // the inventory screen (Phase 6 / ADR-027): Alt+V opens (inVentory —
  // Alt+N has been the voucher screen's new-draft since its first day),
  // Esc is back
  useCommand(
    {
      id: 'inventory.open',
      title: 'Inventory — stock depth: items, positions, BOMs',
      run: () => setScreen('inventory'),
    },
    'Alt+V',
  );
  // the GST compliance screen (Phase 7 / ADR-028): Alt+T opens (T for tax),
  // Esc is back
  useCommand(
    {
      id: 'gst.open',
      title: 'GST compliance — import 2A/2B & reconcile ITC',
      run: () => setScreen('gst'),
    },
    'Alt+T',
  );

  return (
    <>
      {screen === 'voucher' ? (
        <VoucherScreen
          ledgers={ledgers}
          groups={groups}
          apiState={apiState}
          bookName={activeBook?.name ?? null}
          onRetryMasters={() => void loadMasters()}
          onLedgerCreated={(ledger) => setLedgers((prev) => [...prev, ledger])}
          ctxRef={ctxRef}
        />
      ) : screen === 'payslip' ? (
        <PayslipScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'daybook' ? (
        <DaybookScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'plugins' ? (
        <PluginsScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'books' ? (
        <BooksScreen
          activeBookId={activeBook?.id ?? null}
          onSelect={selectBook}
          onExit={() => setScreen('voucher')}
          ctxRef={ctxRef}
        />
      ) : screen === 'periods' ? (
        <PeriodsScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'outstandings' ? (
        <OutstandingsScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'banking' ? (
        <BankingScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'inventory' ? (
        <InventoryScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : screen === 'gst' ? (
        <GstScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : (
        <SlotsScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      )}
      {showPalette && <CommandPalette onClose={() => setShowPalette(false)} ctxRef={paletteCtxRef} />}
    </>
  );
}
