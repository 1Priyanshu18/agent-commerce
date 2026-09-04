"""Real Razorpay test-mode order creation and payment lookup. Never completes a payment
itself — that's a one-time manual step via scripts/live_test_checkout.py. This module only
ever creates orders and reads them back; it must never be imported by anything in the
automated test suite or the eval loop.
"""

from __future__ import annotations

from typing import Any

import razorpay
import razorpay.errors

from .adapter import PaymentFatalError, PaymentRetryableError
from .idempotency import idempotency_key
from .models import OrderRecord, OrderStatus, PaymentRecord, PaymentStatus

_PAYMENT_STATUS_MAP = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "failed": PaymentStatus.FAILED,
    "refunded": PaymentStatus.REFUNDED,
}


def _order_from_api(data: dict, transaction_id: str) -> OrderRecord:
    return OrderRecord(
        order_id=data["id"],
        transaction_id=transaction_id,
        amount_paise=data["amount"],
        currency=data["currency"],
        receipt=data["receipt"],
        status=OrderStatus(data["status"]),
        notes=data.get("notes") or {},
        created_at=str(data["created_at"]),
    )


def _payment_from_api(item: dict) -> PaymentRecord:
    return PaymentRecord(
        payment_id=item["id"],
        order_id=item["order_id"],
        amount_paise=item["amount"],
        currency=item["currency"],
        status=_PAYMENT_STATUS_MAP.get(item["status"], PaymentStatus.FAILED),
        method=item.get("method"),
        captured=bool(item.get("captured", False)),
        error_code=item.get("error_code"),
        error_description=item.get("error_description"),
    )


class RazorpayLiveTestAdapter:
    def __init__(self, *, key_id: str, key_secret: str, client: Any | None = None) -> None:
        self._client = client or razorpay.Client(auth=(key_id, key_secret))

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ) -> OrderRecord:
        receipt = idempotency_key(transaction_id, attempt_no)
        try:
            data = self._client.order.create(
                data={
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": receipt,
                    "notes": {"transaction_id": transaction_id, "policy_version": policy_version},
                }
            )
        except razorpay.errors.BadRequestError as e:
            raise PaymentFatalError(str(e)) from e
        except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as e:
            raise PaymentRetryableError(str(e)) from e
        return _order_from_api(data, transaction_id)

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        try:
            data = self._client.order.payments(order_id)
        except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as e:
            raise PaymentRetryableError(str(e)) from e
        return [_payment_from_api(item) for item in data.get("items", [])]
