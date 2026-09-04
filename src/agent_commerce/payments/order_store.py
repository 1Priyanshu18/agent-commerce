"""Our own local record of orders — the "our order record" side of the reconciler's
three-way match, and the source of truth for order status as reconciliation updates it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import OrderRecord, OrderStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    receipt TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_transaction_id ON orders (transaction_id);
"""


class OrderStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(self, order: OrderRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO orders (
                order_id, transaction_id, amount_paise, currency, receipt, status, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET status = excluded.status
            """,
            (
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

    def save_if_absent(self, order: OrderRecord) -> None:
        """Unlike save(), never overwrites an existing row's status. For callers (like
        RecordingPaymentAdapter) that only need to guarantee an order exists at all — the
        order may already have been saved and even reconciled to a later status (e.g. by the
        simulated adapter, whose webhook fires synchronously inside create_order()) by the
        time this runs, and a blind overwrite would clobber that.
        """
        self._conn.execute(
            """
            INSERT OR IGNORE INTO orders (
                order_id, transaction_id, amount_paise, currency, receipt, status, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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

    def update_status(self, order_id: str, status: OrderStatus) -> None:
        self._conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status.value, order_id))
        self._conn.commit()

    def get(self, order_id: str) -> OrderRecord | None:
        row = self._conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return self._row_to_order(row) if row else None

    def get_by_transaction(self, transaction_id: str) -> OrderRecord | None:
        # ORDER BY rowid, not created_at: two orders can share a timestamp (same-second
        # creation is plausible), and rowid reflects true insertion order unambiguously.
        row = self._conn.execute(
            "SELECT * FROM orders WHERE transaction_id = ? ORDER BY rowid DESC LIMIT 1", (transaction_id,)
        ).fetchone()
        return self._row_to_order(row) if row else None

    def all_pending(self) -> list[OrderRecord]:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE status != ? ORDER BY created_at ASC", (OrderStatus.PAID.value,)
        ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def _row_to_order(self, row: sqlite3.Row) -> OrderRecord:
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
