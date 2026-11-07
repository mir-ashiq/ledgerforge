"""Run engine calls off the GUI thread — one small runner, no asyncio.

``run(fn, on_done, on_error)`` schedules ``fn()`` on the global thread pool
and marshals its result (or exception) back onto the GUI thread via a Qt
signal. The pages stay linear: build request -> run -> fill table.

Lifetime, the hard-won way: the QRunnable's C++ object is deleted by the
pool the instant ``run()`` returns — and a cross-thread ``emit`` only *posts*
an event. If nothing else owns the signal QObject by the time the GUI thread
delivers it, the emit lands in freed memory (intermittent native segfaults,
1-in-3 under load). ``_INFLIGHT`` keeps every task's emitter alive until
AFTER its callback has run on the GUI thread; the wrapper drops it then.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Captured:
    """The exception, carried across threads as plain text + original."""

    def __init__(self, exc: BaseException):
        self.type = type(exc).__name__
        self.detail = str(exc) or type(exc).__name__


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _TaskSignals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self) -> None:  # worker thread
        try:
            result = self.fn()
        except Exception:  # noqa: BLE001 — the GUI shows it, never crashes
            traceback.print_exc()
            self.signals.failed.emit(_Captured(_current_exception()))
        else:
            self.signals.done.emit(result)


def _current_exception() -> BaseException:
    import sys

    return sys.exc_info()[1]  # type: ignore[return-value]


#: task key -> _TaskSignals: keeps emitters alive across the queued hop
_INFLIGHT: dict[int, _TaskSignals] = {}
_next_key = 0


def run(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[_Captured], None] | None = None,
) -> None:
    """Fire ``fn`` on a worker thread; deliver its result on the GUI thread."""
    global _next_key

    signals = _TaskSignals()
    key = _next_key
    _next_key += 1
    _INFLIGHT[key] = signals  # owns the emitter until after delivery

    def _deliver_done(result: Any) -> None:
        try:
            on_done(result)
        finally:
            _INFLIGHT.pop(key, None)

    def _deliver_failed(error: _Captured) -> None:
        try:
            if on_error is not None:
                on_error(error)
        finally:
            _INFLIGHT.pop(key, None)

    signals.done.connect(_deliver_done)
    signals.failed.connect(_deliver_failed)
    QThreadPool.globalInstance().start(_Task(fn, signals))
