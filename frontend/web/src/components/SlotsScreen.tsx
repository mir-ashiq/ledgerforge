/**
 * The slots screen (ADR-019): the host's slot picker — every dashboard a
 * loaded pack declares (GET /api/v1/slots), one row each with its owning
 * pack and trust basis, Tally-style navigation (↑/↓, Enter) and Esc as the
 * universal back. `Alt+U` opens it. Opening a slot mounts the SlotHost —
 * the sandboxed iframe plus the §5.5 postMessage relay.
 */

import { useCallback, useEffect, useState } from 'react';
import { useFocusScope, useRovingFocus } from '@ledgerforge/keyboard-kit/react';
import { api } from '../api/client';
import type { SlotOut } from '../api/types';
import { SlotHost } from './SlotHost';

interface SlotsScreenProps {
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

const TRUST_HINT: Record<SlotOut['trust'], string> = {
  trusted: 'operator channel',
  signed: 'signed drop-in',
  acknowledged: 'operator-pinned',
};

type LoadState = 'loading' | 'error' | 'ready';

export function SlotsScreen({ onExit, ctxRef }: SlotsScreenProps) {
  const [slots, setSlots] = useState<readonly SlotOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [reload, setReload] = useState(0);
  const [active, setActive] = useState<number>(-1);

  // the list is the screen's focus scope: Esc pops it (a mounted slot host
  // is a deeper scope and pops first)
  useFocusScope('slotsScreen', () => {
    onExit();
    return true;
  });

  const load = useCallback(async () => {
    setLoadState('loading');
    try {
      const out = await api.slots();
      setSlots(out.slots);
      setLoadState('ready');
    } catch {
      setLoadState('error');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, reload]);

  const roving = useRovingFocus(slots.length, (index) => setActive(index));

  if (active >= 0 && slots[active]) {
    return <SlotHost slot={slots[active]} onExit={() => setActive(-1)} ctxRef={ctxRef} />;
  }

  return (
    <section className="screen" aria-label="Plugin slots">
      <header className="screen-header">
        <h2>Slots — sandboxed plugin dashboards</h2>
        <p className="hint">
          every loaded pack's declared panels, rendered in a hardened sandbox —
          a slot never touches the host page; it speaks the API through the
          host's relay and registers its commands for the palette
        </p>
      </header>
      {loadState === 'loading' && <p className="banner">loading slots…</p>}
      {loadState === 'error' && (
        <p className="banner error">
          cannot reach the engine — <button onClick={() => setReload((n) => n + 1)}>retry</button>
        </p>
      )}
      {loadState === 'ready' && slots.length === 0 && (
        <p className="banner">
          no pack declares a slot yet — see the plugin guide (ADR-019) for the
          one-attribute manifest declaration
        </p>
      )}
      {loadState === 'ready' && slots.length > 0 && (
        <table className="list" aria-label="Slots">
          <thead>
            <tr>
              <th>Slot</th>
              <th>Pack</th>
              <th>Trust</th>
            </tr>
          </thead>
          <tbody>
            {slots.map((slot, index) => (
              <tr
                key={slot.key}
                tabIndex={0}
                onKeyDown={(event) => roving.onKey(index, event)}
                onClick={() => setActive(index)}
                data-testid={`slot-row-${slot.key}`}
              >
                <td>{slot.title}</td>
                <td>{slot.plugin}</td>
                <td>{TRUST_HINT[slot.trust]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
