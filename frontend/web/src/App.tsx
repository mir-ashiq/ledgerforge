/**
 * Application root: owns the keymap lifecycle, the prefetched masters
 * (ledger + group indexes) and the keyboard-reference overlay. The screen
 * itself is VoucherScreen.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { keymap, useCommand } from '@ledgerforge/keyboard-kit/react';
import { api } from './api/client';
import type { GroupOut, LedgerOut } from './api/types';
import { HelpOverlay } from './components/HelpOverlay';
import { VoucherScreen } from './components/VoucherScreen';

export default function App() {
  const [ledgers, setLedgers] = useState<LedgerOut[]>([]);
  const [groups, setGroups] = useState<GroupOut[]>([]);
  const [apiState, setApiState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [showHelp, setShowHelp] = useState(false);
  const ctxRef = useRef<Record<string, unknown>>({});

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

  // the single capture-phase keydown pipeline (ADR-004)
  useEffect(() => {
    keymap.setContextProvider(() => ctxRef.current);
    keymap.start();
    return () => keymap.stop();
  }, []);

  // global commands
  useCommand(
    { id: 'focus.back', title: 'Back / close (Esc)', run: () => { keymap.focus.escape(); } },
    'Escape',
  );
  useCommand(
    { id: 'palette.toggle', title: 'Keyboard reference (Ctrl+K)', run: () => setShowHelp((v) => !v) },
    'Primary+K',
  );

  return (
    <>
      <VoucherScreen
        ledgers={ledgers}
        groups={groups}
        apiState={apiState}
        onRetryMasters={() => void loadMasters()}
        onLedgerCreated={(ledger) => setLedgers((prev) => [...prev, ledger])}
        ctxRef={ctxRef}
      />
      {showHelp && <HelpOverlay onClose={() => setShowHelp(false)} />}
    </>
  );
}
