"""A deterministic, always-succeeds payment stub — exists only so the orchestrator has
something to call now, matching its role as the sole caller of payments/. Replaced (not
extended) by the real live_test/simulated adapters once the actual Razorpay integration and
adapter interface are designed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PaymentStatus(StrEnum):
    PAID = "paid"


@dataclass(frozen=True)
class PaymentResult:
    status: PaymentStatus
    order_id: str
    payment_id: str


class StubPaymentAdapter:
    def pay(self, *, transaction_id: str, amount_paise: int) -> PaymentResult:
        return PaymentResult(
            status=PaymentStatus.PAID,
            order_id=f"stub_order_{transaction_id}",
            payment_id=f"stub_payment_{transaction_id}",
        )
