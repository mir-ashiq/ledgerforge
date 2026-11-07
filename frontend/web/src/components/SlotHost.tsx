/**
 * SlotHost (ADR-019): renders ONE sandboxed plugin slot and speaks the
 * §5.5 protocol on its behalf. The iframe is `sandbox="allow-scripts"` —
 * opaque origin: no cookies, no localStorage, no DOM access to the host —
 * and the host is the slot's ONLY bridge to the world:
 *
 *   host → slot   lf-slot:init {slot, apiBase}            (on iframe load)
 *   slot → host   lf-slot:register-commands [{id,title,key?}]
 *   host → slot   lf-slot:command {id}                    (host-bound keys)
 *   slot → host   lf-slot:request {id, path}              (GET, versioned API)
 *   host → slot   lf-slot:response {id, ok, status, body}
 *   slot → host   lf-slot:close                           (Esc inside sandbox)
 *
 * M4 + D4: the slot never receives keyboard scope. Its commands are HOST
 * commands — registered through keyboard-kit, bound only while this host is
 * mounted (`when: slotHostActive`), shown in the command palette for free —
 * and keystrokes are routed back into the sandbox as lf-slot:command.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { SlotOut } from '../api/types';

const API_BASE = '/api/v1';

interface SlotCommandIn {
  id: string;
  title: string;
  key?: string;
}

interface SlotHostProps {
  slot: SlotOut;
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

/** One host-registered slot command (hooks must not be called in loops —
 * a component per command keeps the hook count stable per render). */
function SlotCommandBridge({
  command,
  active,
  fire,
}: {
  command: SlotCommandIn;
  active: boolean;
  fire: (id: string) => void;
}) {
  useCommand(
    {
      id: command.id,
      title: command.title,
      run: () => fire(command.id),
    },
    command.key,
    () => active,
  );
  return null;
}

export function SlotHost({ slot, onExit, ctxRef }: SlotHostProps) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const [commands, setCommands] = useState<readonly SlotCommandIn[]>([]);

  const post = useCallback((message: unknown) => {
    frameRef.current?.contentWindow?.postMessage(message, '*');
  }, []);

  // this host is the deepest focus scope: Esc pops back to the slot list
  useFocusScope('slotHost', () => {
    onExit();
    return true;
  });

  useEffect(() => {
    ctxRef.current.slotHostActive = true;
    return () => {
      ctxRef.current.slotHostActive = false;
      setCommands([]);
    };
  }, [ctxRef]);

  // the relay: everything the sandbox may ask of the host, and nothing more
  useEffect(() => {
    const onMessage = async (event: MessageEvent) => {
      if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
      const msg = event.data as Record<string, unknown> | null;
      if (!msg || typeof msg !== 'object') return;
      const type = msg.type as string;

      if (type === 'lf-slot:register-commands' && Array.isArray(msg.commands)) {
        const incoming: SlotCommandIn[] = [];
        for (const raw of msg.commands) {
          const cmd = raw as Partial<SlotCommandIn>;
          if (typeof cmd.id === 'string' && typeof cmd.title === 'string') {
            incoming.push({
              id: cmd.id,
              title: cmd.title,
              key: typeof cmd.key === 'string' && cmd.key ? cmd.key : undefined,
            });
          }
        }
        setCommands(incoming);
      } else if (type === 'lf-slot:close') {
        onExit();
      } else if (type === 'lf-slot:request') {
        const id = msg.id as string;
        const path = msg.path as string;
        // the relay is GET-only and stays inside the versioned API: no
        // schemes, no protocol-relative hosts, no external origins
        const safe = typeof id === 'string' && typeof path === 'string' &&
          path.startsWith('/') && !path.startsWith('//') && !path.includes('\\');
        if (!safe) {
          post({ type: 'lf-slot:response', id, ok: false, status: 0, error: 'refused: path must be a versioned-API path' });
          return;
        }
        try {
          const response = await fetch(`${API_BASE}${path}`, {
            headers: { 'Content-Type': 'application/json' },
          });
          let body: unknown = null;
          try {
            body = await response.json();
          } catch {
            body = await response.text();
          }
          post({ type: 'lf-slot:response', id, ok: response.ok, status: response.status, body });
        } catch {
          post({ type: 'lf-slot:response', id, ok: false, status: 0, error: 'network error' });
        }
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [onExit, post]);

  const fire = useCallback(
    (id: string) => {
      post({ type: 'lf-slot:command', id });
    },
    [post],
  );

  const src = slot.url ?? `${API_BASE}/slots/${encodeURIComponent(slot.key)}`;

  return (
    <section className="screen" aria-label={`Slot: ${slot.title}`}>
      <header className="screen-header">
        <h2>{slot.title}</h2>
        <p className="hint">
          sandboxed slot from <strong>{slot.plugin}</strong> ({slot.trust}) — Esc is back;
          its commands are in the palette{commands.length > 0 ? ': ' + commands.map((c) => c.title).join(' · ') : ''}
        </p>
      </header>
      <iframe
        ref={frameRef}
        title={slot.title}
        src={src}
        sandbox="allow-scripts"
        className="slot-frame"
        onLoad={() => post({ type: 'lf-slot:init', slot: slot.key, apiBase: API_BASE })}
      />
      {commands.map((command) => (
        <SlotCommandBridge
          key={command.id}
          command={command}
          active
          fire={fire}
        />
      ))}
    </section>
  );
}
