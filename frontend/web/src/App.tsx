/**
 * Application root: owns the keymap lifecycle (with user-binding persistence),
 * the prefetched masters (ledger + group indexes), screen switching and the
 * command palette. Screens: VoucherScreen (entry), PayslipScreen (ADR-013)
 * and DaybookScreen (ADR-015).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { keymap, useCommand } from '@ledgerforge/keyboard-kit/react';
import { api } from './api/client';
import type { GroupOut, LedgerOut } from './api/types';
import { CommandPalette } from './components/CommandPalette';
import { DaybookScreen } from './components/DaybookScreen';
import { PayslipScreen } from './components/PayslipScreen';
import { VoucherScreen } from './components/VoucherScreen';
import { localStorageKeymapStore } from './keymapStore';

type Screen = 'voucher' | 'payslip' | 'daybook';

export default function App() {
  const [ledgers, setLedgers] = useState<LedgerOut[]>([]);
  const [groups, setGroups] = useState<GroupOut[]>([]);
  const [apiState, setApiState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [showPalette, setShowPalette] = useState(false);
  const [screen, setScreen] = useState<Screen>('voucher');
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

  useEffect(() => {
    void loadMasters();
  }, [loadMasters]);

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

  return (
    <>
      {screen === 'voucher' ? (
        <VoucherScreen
          ledgers={ledgers}
          groups={groups}
          apiState={apiState}
          onRetryMasters={() => void loadMasters()}
          onLedgerCreated={(ledger) => setLedgers((prev) => [...prev, ledger])}
          ctxRef={ctxRef}
        />
      ) : screen === 'payslip' ? (
        <PayslipScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      ) : (
        <DaybookScreen onExit={() => setScreen('voucher')} ctxRef={ctxRef} />
      )}
      {showPalette && <CommandPalette onClose={() => setShowPalette(false)} ctxRef={paletteCtxRef} />}
    </>
  );
}
