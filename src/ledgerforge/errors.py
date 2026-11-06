"""Engine error taxonomy. Mapped to HTTP status codes in ``main.py``."""

from __future__ import annotations


class EngineError(Exception):
    """Base class for expected, user-facing engine failures."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ValidationError(EngineError):
    """Payload violates an accounting rule (including Law 1)."""

    status_code = 422


class UnbalancedVoucherError(ValidationError):
    """Law 1: total debits do not exactly equal total credits."""


class NotFoundError(EngineError):
    status_code = 404


class ConflictError(EngineError):
    status_code = 409


class PeriodStateError(ConflictError):
    """The voucher date falls in a locked or closed period (issue #5). The
    database posting gate enforces the same rule; this is the friendly,
    pre-emptive check."""


class InvariantViolation(EngineError):
    """An internal invariant failed — must never happen; surfaces as 500."""

    status_code = 500
