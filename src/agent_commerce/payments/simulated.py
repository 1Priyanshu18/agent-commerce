"""Constructs a correctly HMAC-signed webhook payload and feeds it to our own webhook
handler, so the full lifecycle (order -> payment -> webhook -> reconciliation) still executes
and still appears in the ledger, with no real Razorpay backend involved and no public
webhook endpoint needed. Same PaymentAdapter interface as the live_test adapter; the
reconciler never knows which one ran.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from agent_commerce.core.clock import Clock, SystemClock
from agent_commerce.core.ids import generate_id

from .idempotency import idempotency_key
from .models import OrderRecord, OrderStatus, PaymentRecord, PaymentStatus
from .order_store import OrderStore
from .webhook import WebhookHandler


class SimulatedPaymentAdapter:
    def __init__(
        self,
        *,
        webhook_secret: str,
        webhook_handler: WebhookHandler,
        order_store: OrderStore,
        clock: Clock | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._webhook_handler = webhook_handler
        self._order_store = order_store
        self._clock = clock or SystemClock()
        self._payments: dict[str, list[PaymentRecord]] = {}
        self._suppress_webhook_for: set[str] = set()

    def suppress_webhook(self, transaction_id: str) -> None:
        """Test/demo-only: makes the next create_order() for this transaction record the
        payment as captured but skip delivering the webhook, reproducing "payment succeeded,
        our webhook got lost" on demand. Never called by normal code.
        """
        self._suppress_webhook_for.add(transaction_id)

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord:
        order_id = generate_id("order_sim")
        receipt = idempotency_key(transaction_id, attempt_no)
        notes = {"transaction_id": transaction_id, "policy_version": policy_version}
        order = OrderRecord(
            order_id=order_id,
            transaction_id=transaction_id,
            amount_paise=amount_paise,
            currency="INR",
            receipt=receipt,
            status=OrderStatus.CREATED,
            notes=notes,
            created_at=self._clock.now().isoformat(),
        )
        # Must be durably visible before the webhook fires — a real order always exists
        # before any webhook about it can arrive, and reconciliation (triggered by the
        # webhook below) needs to find it.
        self._order_store.save(order)

        payment_id = generate_id("pay_sim")
        payment = PaymentRecord(
            payment_id=payment_id,
            order_id=order_id,
            amount_paise=amount_paise,
            currency="INR",
            status=PaymentStatus.CAPTURED,
            method="card",
            captured=True,
            error_code=None,
            error_description=None,
        )
        self._payments.setdefault(order_id, []).append(payment)

        if transaction_id in self._suppress_webhook_for:
            self._suppress_webhook_for.discard(transaction_id)
        else:
            self._deliver_webhook(order=order, payment=payment, notes=notes)

        return order

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        return list(self._payments.get(order_id, []))

    def _deliver_webhook(self, *, order: OrderRecord, payment: PaymentRecord, notes: dict) -> None:
        body = {
            "entity": "event",
            "account_id": "acc_simulated",
            "event": "payment.captured",
            "contains": ["payment", "order"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment.payment_id,
                        "entity": "payment",
                        "amount": payment.amount_paise,
                        "currency": payment.currency,
                        "status": payment.status.value,
                        "order_id": payment.order_id,
                        "method": payment.method,
                        "captured": payment.captured,
                        "notes": notes,
                    }
                },
                "order": {
                    "entity": {
                        "id": order.order_id,
                        "entity": "order",
                        "amount": order.amount_paise,
                        "currency": order.currency,
                        "receipt": order.receipt,
                        "status": "paid",
                        "notes": notes,
                    }
                },
            },
            "created_at": int(self._clock.now().timestamp()),
        }
        raw_body = json.dumps(body).encode("utf-8")
        signature = hmac.new(self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        self._webhook_handler.handle(raw_body=raw_body, signature=signature)
