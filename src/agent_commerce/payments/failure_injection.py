"""Test/demo-only wrapper that can make the FIRST create_order() call for a specific
transaction fail with a retryable error, so the payment_failure path is reproducible on
demand instead of depending on a real network blip. Never used by build_payment_stack() —
the orchestrator applies this itself, only when a session asks for it
(BuyerSessionRunner.run(inject_failure="payment_failure")).

Sits outside the idempotent/recording wrapping (build_payment_stack()'s composed adapter is
what gets wrapped here), so an injected failure never reaches the idempotency store — nothing
is written for the failed call, and the orchestrator's retry with the same idempotency key
(same transaction_id, same attempt_no) reaches the real adapter cleanly on its first genuine
attempt.
"""

from __future__ import annotations

from typing import Any

from .adapter import PaymentRetryableError
from .models import OrderRecord, PaymentRecord


class FailureInjectingPaymentAdapter:
    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self._armed: set[str] = set()

    def arm_failure(self, transaction_id: str) -> None:
        self._armed.add(transaction_id)

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord:
        if transaction_id in self._armed:
            self._armed.discard(transaction_id)
            raise PaymentRetryableError(
                f"injected failure: simulated gateway timeout for transaction {transaction_id}"
            )
        return self._wrapped.create_order(
            transaction_id=transaction_id,
            amount_paise=amount_paise,
            policy_version=policy_version,
            attempt_no=attempt_no,
        )

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        return self._wrapped.fetch_payments(order_id)
