"""Wraps any PaymentAdapter with idempotency on (transaction_id, attempt_no) — a retry after
a network timeout returns the already-created order instead of creating a second one. Applies
uniformly to both live_test and simulated, same decorator pattern as core/llm's
CachingLLMClient/GuardedLLMClient.
"""

from __future__ import annotations

from typing import Any

from .idempotency import IdempotencyStore, idempotency_key
from .models import OrderRecord, PaymentRecord


class IdempotentPaymentAdapter:
    def __init__(self, wrapped: Any, store: IdempotencyStore) -> None:
        self._wrapped = wrapped
        self._store = store

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord:
        key = idempotency_key(transaction_id, attempt_no)
        existing = self._store.get(key)
        if existing is not None:
            return existing

        order = self._wrapped.create_order(
            transaction_id=transaction_id,
            amount_paise=amount_paise,
            policy_version=policy_version,
            attempt_no=attempt_no,
        )
        self._store.put(key, order)
        return order

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        return self._wrapped.fetch_payments(order_id)
