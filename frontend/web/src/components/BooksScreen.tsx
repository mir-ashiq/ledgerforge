/**
 * The books screen (ADR-009 in the UI): TallyPrime's "select company" and
 * company management. `Alt+O` opens it; the list carries every book
 * (archived last — the engine's own order), `↑/↓` walk it, `Enter` switches
 * the active book (the `X-Book-Id` the client sends on every request from
 * then on), `C` creates a book (seeded with the TallyPrime chart of
 * accounts in one action), `R` renames, `A` archives/restores, `Esc` is
 * back to entry — the same keyboard contract as every LedgerForge screen.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useCommand, useFocusScope } from '@ledgerforge/keyboard-kit/react';
import type { ContextPredicate } from '@ledgerforge/keyboard-kit';
import { api } from '../api/client';
import type { BookOut } from '../api/types';

type LoadState = 'loading' | 'error' | 'ready';

interface BooksScreenProps {
  /** The book every request currently acts on (null = server auto-pick). */
  activeBookId: number | null;
  /** Switch the active book — App reloads masters and remembers it. */
  onSelect: (book: BookOut) => void;
  onExit: () => void;
  /** Live interaction flags, read by keymap `when` predicates. */
  ctxRef: React.MutableRefObject<Record<string, unknown>>;
}

/** True while the list (not a dialog) owns the keys. */
const inBooks: ContextPredicate = (ctx) => ctx.scopeId === 'booksScreen' && ctx.dialogOpen !== true;

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

export function BooksScreen({ activeBookId, onSelect, onExit, ctxRef }: BooksScreenProps) {
  const [books, setBooks] = useState<readonly BookOut[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [index, setIndex] = useState(0);
  const [reload, setReload] = useState(0);
  const [dialog, setDialog] = useState<'create' | { rename: BookOut } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [announce, setAnnounce] = useState('');

  // this screen is the top focus scope: Esc pops it (the universal back)
  useFocusScope('booksScreen', () => {
    if (dialog) {
      setDialog(null);
      return false; // the dialog's own scope stays popped by its Esc
    }
    onExit();
    return true;
  });

  ctxRef.current = { booksOpen: true, dialogOpen: dialog !== null };
  useEffect(
    () => () => {
      ctxRef.current = {};
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoadState('loading');
    api
      .books()
      .then((list) => {
        if (cancelled) return;
        setBooks(list);
        setLoadState('ready');
      })
      .catch(() => {
        if (!cancelled) setLoadState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [reload]);

  const selected = books[index] ?? null;

  const select = useCallback(
    (book: BookOut) => {
      if (book.is_archived) {
        setNote('An archived book is read-only history — restore it (A) before working in it.');
        return;
      }
      onSelect(book);
      onExit();
    },
    [onSelect, onExit],
  );

  const archiveToggle = useCallback(
    async (book: BookOut) => {
      setNote(null);
      try {
        const updated = await api.updateBook(book.id, { is_archived: !book.is_archived });
        setBooks((prev) => prev.map((b) => (b.id === updated.id ? updated : b)));
        setAnnounce(updated.is_archived ? `Archived ${updated.name}` : `Restored ${updated.name}`);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Could not update the book';
        setNote(message);
        setAnnounce(message);
      }
    },
    [],
  );

  const move = useCallback(
    (delta: number) => {
      setIndex((i) => Math.min(books.length - 1, Math.max(0, i + delta)));
    },
    [books.length],
  );

  // keep the highlighted row in view, daybook-style
  useEffect(() => {
    document.querySelector(`[data-book-row="${index}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [index]);

  // --- commands (the list owns the keys while no dialog is open) -------------
  useCommand(
    { id: 'books.up', title: 'Books: previous book', run: () => move(-1) },
    'ArrowUp',
    inBooks,
  );
  useCommand(
    { id: 'books.down', title: 'Books: next book', run: () => move(1) },
    'ArrowDown',
    inBooks,
  );
  useCommand(
    {
      id: 'books.select',
      title: 'Books: switch to the highlighted book',
      run: () => {
        if (selected) select(selected);
      },
    },
    'Enter',
    inBooks,
  );
  useCommand(
    {
      id: 'books.create',
      title: 'Books: create a book (seeded with the chart of accounts)',
      run: () => setDialog('create'),
    },
    'c',
    inBooks,
  );
  useCommand(
    {
      id: 'books.rename',
      title: 'Books: rename the highlighted book',
      run: () => {
        if (selected) setDialog({ rename: selected });
      },
    },
    'r',
    inBooks,
  );
  useCommand(
    {
      id: 'books.archive',
      title: 'Books: archive / restore the highlighted book',
      run: () => {
        if (selected) void archiveToggle(selected);
      },
    },
    'a',
    inBooks,
  );
  useCommand(
    { id: 'books.refresh', title: 'Books: refresh the list', run: () => setReload((n) => n + 1) },
    'F5',
    inBooks,
  );

  // --- render -----------------------------------------------------------------
  return (
    <div className="screen">
      <header className="bar">
        <span className="brand">LedgerForge</span>
        <span className="mode">Books — companies</span>
        <span className="spacer" />
        <button type="button" className="date-chip" onClick={onExit}>
          Voucher entry <kbd>Esc</kbd>
        </button>
      </header>

      <main className="plugins-body">
        {loadState === 'loading' && (
          <p className="payslip-note" role="status">
            Loading books…
          </p>
        )}
        {loadState === 'error' && (
          <p className="payslip-note bad" role="alert">
            Cannot reach the engine — is it running? (make dev)
          </p>
        )}
        {loadState === 'ready' && books.length === 0 && (
          <section className="payslip-card plugins-card" aria-label="No books yet">
            <div className="payslip-card-head">
              <strong>No books yet</strong>
            </div>
            <p className="payslip-note">
              A book is a company's complete accounting context. Create the first one —
              it arrives seeded with the TallyPrime chart of accounts (28 groups, system
              ledgers, 10 voucher types) and posting can begin immediately.
            </p>
            <button type="button" className="payslip-print" onClick={() => setDialog('create')}>
              Create the first book <kbd>C</kbd>
            </button>
          </section>
        )}

        {books.length > 0 && (
          <section className="payslip-card plugins-card" aria-label="Books">
            <div className="payslip-card-head">
              <strong>
                {books.length} book{books.length === 1 ? '' : 's'}
              </strong>
              <button
                type="button"
                className="daybook-reload"
                aria-label="Refresh the books list"
                onClick={() => setReload((n) => n + 1)}
              >
                Refresh
              </button>
            </div>
            <div className="daybook-list" role="grid" aria-label="Books">
              <div className="daybook-row daybook-head" role="row">
                <span role="columnheader">Name</span>
                <span role="columnheader">Currency</span>
                <span role="columnheader">FY starts</span>
                <span role="columnheader">Status</span>
                <span role="columnheader">Created</span>
                <span role="columnheader" />
              </div>
              {books.map((b, i) => (
                <div
                  key={b.id}
                  role="row"
                  aria-selected={i === index}
                  data-book-row={i}
                  className={`daybook-row${i === index ? ' on' : ''}`}
                  onClick={() => setIndex(i)}
                  onDoubleClick={() => select(b)}
                >
                  <span role="gridcell" className="daybook-narr">
                    {b.name}
                  </span>
                  <span role="gridcell" className="mono">
                    {b.base_currency}
                  </span>
                  <span role="gridcell">{MONTHS[b.fy_start_month - 1]}</span>
                  <span role="gridcell" className="daybook-chips">
                    {b.id === activeBookId && <span className="daybook-chip">active</span>}
                    {b.is_archived && <span className="daybook-chip rev">archived</span>}
                  </span>
                  <span role="gridcell" className="mono">
                    {b.created_at.slice(0, 10)}
                  </span>
                  <span role="gridcell">
                    <button
                      type="button"
                      className="daybook-reload"
                      disabled={b.is_archived}
                      onClick={() => select(b)}
                    >
                      Switch
                    </button>
                  </span>
                </div>
              ))}
            </div>
            <p className="payslip-note">
              Switching remembers the book for next launch and reloads the chart of
              accounts; archived books stay browsable through the API (auditor path) but
              refuse work — restore with <code>A</code>.
            </p>
          </section>
        )}

        {note && (
          <p className="payslip-note bad" role="alert">
            {note}
          </p>
        )}
      </main>

      <footer className="daybook-hud" aria-hidden="true">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> move · <kbd>Enter</kbd> switch
        </span>
        <span>
          <kbd>C</kbd> create · <kbd>R</kbd> rename · <kbd>A</kbd> archive/restore
        </span>
        <span>
          <kbd>Esc</kbd> back
        </span>
      </footer>

      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      {dialog === 'create' && (
        <BookDialog
          mode="create"
          onCancel={() => setDialog(null)}
          onDone={(book) => {
            setDialog(null);
            setBooks((prev) => [...prev.filter((b) => b.id !== book.id), book]);
            setIndex(books.length); // the new row
            onSelect(book); // a fresh book is the one to work in …
            onExit(); // … immediately: create drops you into the new book
          }}
        />
      )}
      {dialog !== null && dialog !== 'create' && 'rename' in dialog && (
        <BookDialog
          mode="rename"
          book={dialog.rename}
          onCancel={() => setDialog(null)}
          onDone={(book) => {
            setDialog(null);
            setBooks((prev) => prev.map((b) => (b.id === book.id ? book : b)));
            setAnnounce(`Renamed to ${book.name}`);
          }}
        />
      )}
    </div>
  );
}

/** Create / rename a book. Enter accepts, Esc cancels. */
function BookDialog({
  mode,
  book,
  onCancel,
  onDone,
}: {
  mode: 'create' | 'rename';
  book?: BookOut;
  onCancel: () => void;
  onDone: (book: BookOut) => void;
}) {
  const [name, setName] = useState(book?.name ?? '');
  const [currency, setCurrency] = useState(book?.base_currency ?? 'INR');
  const [fyStart, setFyStart] = useState(book ? String(book.fy_start_month) : '4');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useFocusScope('bookDialog', () => {
    onCancel();
    return true;
  });

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const submit = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError('A book needs a name');
      return;
    }
    setBusy(true);
    setError('');
    try {
      if (mode === 'create') {
        const month = Number.parseInt(fyStart, 10);
        const out = await api.createBook({
          name: trimmed,
          base_currency: currency.trim().toUpperCase() || 'INR',
          ...(Number.isFinite(month) && month >= 1 && month <= 12 ? { fy_start_month: month } : {}),
        });
        onDone(out.book);
      } else if (book) {
        onDone(await api.updateBook(book.id, { name: trimmed }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The engine refused the request');
    } finally {
      setBusy(false);
    }
  }, [name, currency, fyStart, mode, book, onDone]);

  useCommand(
    { id: 'dialog.book.accept', title: mode === 'create' ? 'Create the book' : 'Rename the book', run: () => void submit() },
    'Enter',
    (ctx) => ctx.scopeId === 'bookDialog',
  );

  return (
    <div className="overlay" role="presentation">
      <div className="overlay-card" role="dialog" aria-label={mode === 'create' ? 'Create a book' : 'Rename the book'}>
        <h2>{mode === 'create' ? 'Create a book' : 'Rename the book'}</h2>
        <label className="dialog-field">
          Name
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setError('');
            }}
            aria-label="Book name"
            placeholder="Acme Traders Pvt Ltd"
            aria-invalid={error !== ''}
          />
        </label>
        {mode === 'create' && (
          <div className="dialog-fields-row">
            <label className="dialog-field">
              Base currency
              <input
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
                aria-label="Base currency (ISO 4217)"
                placeholder="INR"
                maxLength={3}
                className="mono"
              />
            </label>
            <label className="dialog-field">
              FY starts in
              <select
                value={fyStart}
                onChange={(e) => setFyStart(e.target.value)}
                aria-label="Fiscal year start month"
              >
                {MONTHS.map((m, i) => (
                  <option key={m} value={String(i + 1)}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <p className="overlay-foot">
          Enter {mode === 'create' ? 'create' : 'rename'} · Esc cancel
          {mode === 'create' && ' — the new book is seeded with the TallyPrime chart of accounts'}
        </p>
        <div className="overlay-actions">
          <button type="button" onClick={() => void submit()} disabled={busy}>
            {busy ? 'Working…' : mode === 'create' ? 'Create' : 'Rename'}
          </button>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
