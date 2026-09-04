"""Idempotency for order creation: a stable key from (transaction_id, attempt_no). A retry
after a network timeout must not create a second order — this store is checked before any
adapter's create_order() runs (see IdempotentPaymentAdapter).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import OrderRecord, OrderStatus


def idempotency_key(transaction_id: str, attempt_no: int) -> str:
    return f"{transaction_id}:{attempt_no}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    receipt TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class IdempotencyStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, key: str) -> OrderRecord | None:
        row = self._conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        return OrderRecord(
            order_id=record["order_id"],
            transaction_id=record["transaction_id"],
            amount_paise=record["amount_paise"],
            currency=record["currency"],
            receipt=record["receipt"],
            status=OrderStatus(record["status"]),
            notes=json.loads(record["notes"]),
            created_at=record["created_at"],
        )

    def put(self, key: str, order: OrderRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO idempotency_keys (
                idempotency_key, order_id, transaction_id, amount_paise, currency, receipt,
                status, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                order.order_id,
                order.transaction_id,
                order.amount_paise,
                order.currency,
                order.receipt,
                order.status.value,
                json.dumps(order.notes),
                order.created_at,
            ),
        )
        self._conn.commit()
