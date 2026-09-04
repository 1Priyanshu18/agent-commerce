"""Three-way match between our order record, a fresh GET of the order's payments (via the
adapter — never a direct Razorpay call, so this stays PAYMENT_MODE-agnostic), and the
webhooks we've received. Runs on webhook receipt and on a timer, so a dropped webhook is
caught by polling instead of stranding the order.
"""

from __future__ import annotations

from typing import Any

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore

from .models import OrderStatus, PaymentStatus, ReconciliationResult, ReconciliationStatus
from .order_store import OrderStore
from .webhook_store import WebhookStore

_CAPTURED_EVENTS = {"payment.captured", "order.paid"}


class Reconciler:
    def __init__(
        self,
        *,
        adapter: Any,
        order_store: OrderStore,
        webhook_store: WebhookStore,
        ledger: LedgerStore,
    ) -> None:
        self._adapter = adapter
        self._order_store = order_store
        self._webhook_store = webhook_store
        self._ledger = ledger

    def reconcile(self, order_id: str) -> ReconciliationResult:
        order = self._order_store.get(order_id)
        if order is None:
            result = ReconciliationResult(
                transaction_id="unknown",
                order_id=order_id,
                status=ReconciliationStatus.MISMATCH,
                human_reason=f"no local order record found for {order_id}",
            )
            self._log(result)
            return result

        fresh_payments = self._adapter.fetch_payments(order_id)
        webhooks = self._webhook_store.get_for_order(order_id)

        payments_say_captured = any(p.status == PaymentStatus.CAPTURED for p in fresh_payments)
        webhook_says_captured = any(w.event in _CAPTURED_EVENTS for w in webhooks)

        if payments_say_captured and webhook_says_captured:
            status = ReconciliationStatus.MATCHED
            reason = (
                f"order {order_id}: a captured payment and a matching webhook both confirm "
                "this order is paid"
            )
            self._order_store.update_status(order_id, OrderStatus.PAID)
        elif not fresh_payments and not webhooks:
            status = ReconciliationStatus.PENDING
            reason = f"order {order_id}: no payment or webhook received yet"
        elif payments_say_captured != webhook_says_captured:
            status = ReconciliationStatus.MISMATCH
            reason = (
                f"order {order_id}: payment records and webhooks disagree on capture status "
                f"(payments_say_captured={payments_say_captured}, "
                f"webhook_says_captured={webhook_says_captured})"
            )
        else:
            status = ReconciliationStatus.PENDING
            reason = f"order {order_id}: activity seen but not yet confirmed captured"

        result = ReconciliationResult(
            transaction_id=order.transaction_id, order_id=order_id, status=status, human_reason=reason
        )
        self._log(result)
        return result

    def reconcile_all_pending(self) -> list[ReconciliationResult]:
        return [self.reconcile(order.order_id) for order in self._order_store.all_pending()]

    def _log(self, result: ReconciliationResult) -> None:
        self._ledger.append(
            transaction_id=result.transaction_id,
            caused_by=[],
            actor=Actor.PAYMENT_LAYER,
            action_type=ActionType.RECONCILIATION,
            input={"order_id": result.order_id},
            output={"status": result.status.value},
            machine_reason=f"RECONCILIATION_{result.status.value.upper()}",
            human_reason=result.human_reason,
        )
