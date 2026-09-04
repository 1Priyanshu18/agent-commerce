"""The one interface both payment adapters (live_test, simulated) satisfy identically. The
reconciler and orchestrator depend only on this — never on which concrete adapter is active.
If either of them needs to know that, the abstraction has failed.
"""

from __future__ import annotations

from typing import Protocol

from .models import OrderRecord, PaymentRecord


class PaymentRetryableError(Exception):
    """A transient failure (5xx, gateway error, timeout) — safe to retry."""


class PaymentFatalError(Exception):
    """A non-retryable failure (bad request, duplicate receipt) — never retried."""


class PaymentAdapter(Protocol):
    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord: ...

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]: ...
