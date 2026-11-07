# ADR-022: The native shell — a Qt window, not a browser tab

- Status: Accepted (v0.18.0)
- Supersedes: the *delivery mechanism* of ADR-021 (the engine stack stays)
- Date: 2026-11-07

## Context

ADR-021 shipped the desktop edition as "embedded engine + web UI on a
loopback port, opened in the default browser." That is a *local web app*,
and users said so in one line: *"I asked for a full Windows application,
not an installer that runs it in a website."* They are right. A browser tab
has a browser's chrome, a URL, a port that can collide, and the constant
temptation to bookmark the wrong thing. A Windows application has a window,
a taskbar presence, menus, and file dialogs of its own.

The engine itself — embedded PostgreSQL (pgserver), the SQL bootstrap, the
FastAPI app, every law, trigger and report — is proven (v0.17's smoke gate,
the v0.16 production hardening, the full test suite). Throwing it away to
write a WinForms front end would discard exactly the part that works.

## Decision

**Keep the ADR-021 engine, replace the browser with a native Qt 6 shell**
(PySide6): `ledgerforge.nativeapp` + `LedgerForge.exe`.

1. **The window is the product.** One QMainWindow — sidebar navigation
   (Day Book, Masters, Reports, Periods, Sandbox), a native menu bar, a
   status bar carrying the engine's heartbeat, native file dialogs for
   PDFs and CSV exports, a real taskbar icon. **No browser is launched,
   no URL is opened, no web UI ships in the bundle.** The exe is a
   GUI-subsystem binary: no console window either.
2. **The engine runs in a child process, not a thread.** The first cut
   ran uvicorn + psycopg inside the Qt process and segfaulted —
   intermittently, 1-in-3 under concurrent load, inside psycopg's rollback
   (faulthandler pinned the dying thread). The same 480-request storms with
   Qt absent: clean. Qt's event loop and libpq share one address space
   badly. `multiprocessing` (spawn) gives the engine its own process:
   no shared heap, no shared signal handlers, and the GUI can never crash
   with the engine or vice versa. `freeze_support()` makes spawn work in
   the frozen exe; a watcher thread shuts the engine down gracefully and
   detects an orphaned parent; a hard kill leaves PostgreSQL to recover
   from WAL exactly as ADR-021 promised.
3. **The engine's loop is `asyncio`, not uvloop** (also in the shared
   `build_server`): libuv's signal/FD grabbing has no business in a
   GUI-adjacent process. Loopback single-user throughput doesn't care.
4. **Qt threading discipline, the hard-won rule.** Engine calls run on
   `QThreadPool` workers; results cross to the GUI thread via signals.
   A QRunnable's C++ object dies the instant `run()` returns while a
   cross-thread `emit` only *posts* an event — the emitter must be kept
   alive until after delivery (a module-level in-flight registry), or the
   emit lands in freed memory. This was the actual segfault; the child
   process was found *after*, and both stay: defense in depth.
5. **The same API, one code path.** The Qt shell is an API client
   (`nativeapp/client.py`) like the web UI — `X-Book-Id` and all. Masters,
   vouchers, reports, periods, slots: no business logic is duplicated into
   the GUI; the engine's own error text ("debits must equal credits")
   surfaces in the dialogs. Amounts stay strings on the wire and
   `Decimal` the moment they land (Law 3).
6. **The smoke gate proves the window.** `LedgerForge.exe --smoke` runs
   under `QT_QPA_PLATFORM=offscreen`: the engine child boots, the main
   window constructs, the day book loads, a trial balance renders, the
   GSTR-1 pack answers — exit 0 or the release does not ship. Two boots
   against one data dir (fresh initdb, then the migrate path) are covered
   by `tests/test_native_smoke.py`.
7. **Keyboard-first survives the platform change.** Ctrl+N posts a voucher
   from anywhere, F5 refreshes the page in focus, Alt+1..5 navigates,
   Ctrl+Q quits, Enter opens the selected voucher — ADR-004's identity in
   a native frame.

## Consequences

- The Windows download shrinks (no bundled web UI) and starts as a real
  program: splash → window, one process pair, data in
  `%LOCALAPPDATA%\LedgerForge` as before.
- The web UI remains a first-class deployment (Docker, network, E2E) —
  unchanged. ADR-021's engine stack is the native app's stack.
- Linux/macOS run the same code from source (`ledgerforge-native`,
  `python -m ledgerforge.nativeapp`); only Windows is packaged today,
  exactly as ADR-021 scoped it.
- The engine child adds one process to the desktop footprint and ~1s of
  spawn latency on boot — the splash window owns that wait honestly.
- PySide6 joins the `gui` extra (and `dev`, so the suite runs the GUI
  tests); Qt's system libraries are installed on the CI runner; the GUI
  tests run offscreen everywhere, including the Windows release gate.
