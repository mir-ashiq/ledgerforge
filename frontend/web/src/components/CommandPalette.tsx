/**
 * Command palette (Ctrl+K) — keyboard-ux §8 phase 3.
 *
 * One overlay, three verbs: *run* a command (Enter), *rebind* it (Ctrl+R,
 * then press the new combo), or *reset* it to the shipped binding (Alt+R).
 * The list always reads the live keymap — including user rebindings — and
 * rebindings persist through the attached KeymapStore. While a rebind
 * capture is active the keymap pipeline is inert (KeymapService capture
 * mode), so defining a combo can never trigger the command it used to fire.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { formatBinding, parseBindingSpec, type Command, type ContextPredicate } from '@ledgerforge/keyboard-kit';
import { keymap, useCommand, useFocusScope, useKeymapVersion } from '@ledgerforge/keyboard-kit/react';

interface PaletteProps {
  onClose: () => void;
  /** Live flags for this surface, merged into the keymap context by App. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

interface Row {
  command: Command;
  /** Platform-normalized display form ('Ctrl+A', not the raw 'Primary+A'). */
  spec: string;
  isUser: boolean;
  displaced: boolean;
}

function displaySpec(spec: string): string {
  try {
    return formatBinding(parseBindingSpec(spec));
  } catch {
    return spec; // raw fallback: specs are validated before they get here
  }
}

/** Same ladder as the ledger picker: exact → prefix → word-boundary → substring. */
function scoreCommand(row: Row, query: string): number | null {
  const q = query.trim().toLowerCase();
  if (q === '') return 50;
  const title = row.command.title.toLowerCase();
  const hay = `${title} ${row.spec.toLowerCase()} ${row.command.id.toLowerCase()}`;
  if (title === q) return 0;
  if (title.startsWith(q)) return 10;
  const idx = hay.indexOf(q);
  if (idx === -1) return null;
  return idx > 0 && hay[idx - 1] === ' ' ? 20 : 30;
}

function rankCommands(rows: readonly Row[], query: string): Row[] {
  return rows
    .map((row) => ({ row, s: scoreCommand(row, query) }))
    .filter((r): r is { row: Row; s: number } => r.s !== null)
    .sort((a, b) => a.s - b.s || a.row.command.title.localeCompare(b.row.command.title))
    .map((r) => r.row);
}

export function CommandPalette({ onClose, ctxRef }: PaletteProps) {
  const [query, setQuery] = useState('');
  const [highlight, setHighlight] = useState(0);
  const [rebinding, setRebinding] = useState<string | null>(null); // command id
  const [note, setNote] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const rebindingRef = useRef<string | null>(null);
  rebindingRef.current = rebinding;
  const version = useKeymapVersion(); // rows re-derive on every binding change

  useFocusScope('commandPalette', () => {
    onClose();
    return true;
  });

  // this surface's live flags (read by `when` predicates at keydown time)
  ctxRef.current = { paletteOpen: true };

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    setHighlight(0);
  }, [query]);

  useEffect(() => {
    document
      .querySelector<HTMLElement>(`[data-cmd-idx="${highlight}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [highlight]);

  // end capture mode no matter how the palette closes
  useEffect(
    () => () => {
      keymap.endCapture();
      ctxRef.current = {};
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const rows = useMemo<Row[]>(() => {
    void version; // re-derive when bindings change
    return keymap.commands.list().flatMap((command) => {
      const info = keymap.bindingInfo(command.id);
      if (!info) return []; // registered but never bound (palette-only commands)
      return [{
        command,
        spec: displaySpec(info.spec),
        isUser: info.isUser,
        displaced: info.displaced ?? false,
      }];
    });
  }, [version]);

  const visible = useMemo(() => rankCommands(rows, query), [rows, query]);
  const rebindingCommand = rebinding ? keymap.commands.get(rebinding) : undefined;

  // ---- palette commands (scoped: deepest scope is 'commandPalette') ---------
  const inPalette: ContextPredicate = (ctx) => ctx.scopeId === 'commandPalette';

  useCommand(
    {
      id: 'palette.next',
      title: 'Palette: next match',
      run: () => setHighlight((h) => (visible.length ? (h + 1) % visible.length : 0)),
    },
    'ArrowDown',
    inPalette,
  );

  useCommand(
    {
      id: 'palette.previous',
      title: 'Palette: previous match',
      run: () => setHighlight((h) => (visible.length ? (h - 1 + visible.length) % visible.length : 0)),
    },
    'ArrowUp',
    inPalette,
  );

  useCommand(
    {
      id: 'palette.run',
      title: 'Palette: run highlighted command',
      run: () => {
        const row = visible[highlight];
        if (!row) return;
        onClose();
        void keymap.execute(row.command.id);
      },
    },
    'Enter',
    inPalette,
  );

  useCommand(
    {
      id: 'palette.rebind',
      title: 'Palette: rebind highlighted command',
      run: () => {
        const row = visible[highlight];
        if (!row) return;
        keymap.beginCapture();
        setRebinding(row.command.id);
        setNote({ kind: 'info', text: `Press the new keys for “${row.command.title}” — Esc cancels` });
      },
    },
    'Primary+R',
    inPalette,
  );

  useCommand(
    {
      id: 'palette.resetBinding',
      title: 'Palette: reset highlighted binding',
      run: () => {
        const row = visible[highlight];
        if (!row || !row.isUser) {
          setNote({ kind: 'info', text: 'Already on the shipped binding' });
          return;
        }
        keymap.resetKeybinding(row.command.id);
        const restored = keymap.bindingInfo(row.command.id)?.spec ?? 'its default';
        setNote({ kind: 'ok', text: `“${row.command.title}” back on ${restored}` });
      },
    },
    'Alt+R',
    inPalette,
  );

  // ---- rebind capture: the next keydown is data, not a command ---------------
  function onCaptureKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    event.preventDefault();
    event.stopPropagation();
    const commandId = rebindingRef.current;
    if (event.key === 'Escape' || !commandId) {
      keymap.endCapture();
      setRebinding(null);
      setNote({ kind: 'info', text: 'Rebind cancelled' });
      return;
    }
    if (['Control', 'Meta', 'Alt', 'Shift'].includes(event.key)) return; // modifier held
    if (event.key === 'Tab') return; // keep an accessibility exit available
    const combo = {
      key: event.key.length === 1 ? event.key.toLowerCase() : event.key,
      ctrl: event.ctrlKey,
      meta: event.metaKey,
      alt: event.altKey,
      shift: event.shiftKey,
    };
    const command = keymap.commands.get(commandId);
    if (!command) return;
    if (combo.key.length === 1 && !combo.ctrl && !combo.meta && !combo.alt) {
      setNote({ kind: 'err', text: 'Plain letter/number keys are for typing — add Ctrl, Alt or Cmd' });
      return;
    }
    const spec = formatBinding([combo]);
    // who loses this combo? (a combo triggers exactly one command)
    const losers = rows.filter(
      (row) =>
        row.command.id !== commandId &&
        !row.displaced &&
        formatBinding(parseBindingSpec(row.spec)) === spec,
    );
    try {
      keymap.setKeybinding(spec, commandId);
    } catch (err) {
      setNote({ kind: 'err', text: err instanceof Error ? err.message : 'Rebind failed' });
      return;
    }
    keymap.endCapture();
    setRebinding(null);
    const moved = losers.length ? ` (${losers.map((l) => `“${l.command.title}”`).join(', ')} moved off it)` : '';
    setNote({ kind: 'ok', text: `“${command.title}” rebound to ${spec}${moved}` });
    setQuery('');
    setHighlight(0);
    inputRef.current?.focus();
  }

  // ---- render -------------------------------------------------------------------
  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card palette" role="dialog" aria-label="Command palette">
        <h2>Command palette</h2>
        <input
          ref={inputRef}
          type="text"
          className={rebinding ? 'capturing' : ''}
          value={query}
          placeholder={rebinding ? `Press new keys for “${rebindingCommand?.title ?? rebinding}”…` : 'Type to search commands — Enter runs, Ctrl+R rebinds, Alt+R resets'}
          aria-label="Search commands"
          autoComplete="off"
          spellCheck={false}
          readOnly={rebinding !== null}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (rebinding !== null) onCaptureKeyDown(e);
          }}
        />
        {note && (
          <p className={`palette-note ${note.kind}`} role="status">
            {note.text}
          </p>
        )}
        <div className="palette-list" role="listbox" aria-label="Commands">
          {visible.map((row, i) => (
            <div
              key={row.command.id}
              role="option"
              aria-selected={i === highlight}
              data-cmd-idx={i}
              className={`palette-row${i === highlight ? ' hl' : ''}`}
              onMouseDown={(e) => {
                e.preventDefault(); // keep focus in the search box
                setHighlight(i);
              }}
            >
              <span className="palette-title">{row.command.title}</span>
              <span className="palette-keys">
                {row.displaced && <em className="palette-flag conflict">conflicts</em>}
                {row.isUser && <em className="palette-flag custom">custom</em>}
                <kbd className={row.displaced ? 'struck' : ''}>{row.spec}</kbd>
              </span>
            </div>
          ))}
          {visible.length === 0 && <p className="palette-empty">No matching command.</p>}
        </div>
        <p className="overlay-foot">
          <kbd>Enter</kbd> run · <kbd>Ctrl+R</kbd> rebind · <kbd>Alt+R</kbd> reset · <kbd>Esc</kbd> close
        </p>
        <div className="sr-only" aria-live="polite">
          {note?.text ?? ''}
        </div>
      </div>
    </div>
  );
}
