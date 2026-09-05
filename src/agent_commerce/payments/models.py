from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    transaction_id: str
    amount_paise: int
    currency: str
    receipt: str
    status: OrderStatus
    notes: dict
    created_at: str


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class PaymentRecord:
    payment_id: str
    order_id: str
    amount_paise: int
    currency: str
    status: PaymentStatus
    method: str | None
    captured: bool
    error_code: str | None
    error_description: str | None


class ReconciliationStatus(StrEnum):
    MATCHED = "matched"
    PENDING = "pending"
    PENDING_RECONCILIATION = "pending_reconciliation"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class ReconciliationResult:
    transaction_id: str
    order_id: str
    status: ReconciliationStatus
    human_reason: str
