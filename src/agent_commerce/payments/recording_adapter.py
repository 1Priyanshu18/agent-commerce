"""Persists every order create_order() returns into the OrderStore — the "our order record"
side of the reconciler's three-way match. Applies uniformly regardless of which concrete
adapter is wrapped, same decorator pattern as IdempotentPaymentAdapter.

Uses save_if_absent(), not save(): for the simulated adapter, the order may already have been
saved and even reconciled to a later status by the time create_order() returns (its webhook
fires synchronously, inside the call), and this must not clobber that.
"""

from __future__ import annotations

from typing import Any

from .models import OrderRecord, PaymentRecord
from .order_store import OrderStore


class RecordingPaymentAdapter:
    def __init__(self, wrapped: Any, order_store: OrderStore) -> None:
        self._wrapped = wrapped
        self._order_store = order_store

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord:
        order = self._wrapped.create_order(
            transaction_id=transaction_id,
            amount_paise=amount_paise,
            policy_version=policy_version,
            attempt_no=attempt_no,
        )
        self._order_store.save_if_absent(order)
        return order

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        return self._wrapped.fetch_payments(order_id)
