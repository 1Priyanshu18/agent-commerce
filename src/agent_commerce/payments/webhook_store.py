"""Stores every received webhook's raw payload, idempotent on (event, payment_id) —
duplicates and out-of-order delivery are normal for webhooks and must not be treated as new
events."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent_commerce.core.clock import Clock, SystemClock
from agent_commerce.core.ids import generate_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
    webhook_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    order_id TEXT,
    raw_body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (event, payment_id)
);
CREATE INDEX IF NOT EXISTS idx_webhooks_order_id ON webhooks (order_id);
"""


@dataclass(frozen=True)
class WebhookRecord:
    webhook_id: str
    event: str
    payment_id: str
    order_id: str | None
    raw_body: str
    received_at: str


class WebhookStore:
    def __init__(self, db_path: str | Path, clock: Clock | None = None) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = clock or SystemClock()

    def save_if_new(
        self, *, event: str, payment_id: str, order_id: str | None, raw_body: str
    ) -> tuple[WebhookRecord, bool]:
        """Returns (record, is_new). A duplicate (event, payment_id) returns the existing
        record with is_new=False rather than raising or inserting again.
        """
        existing = self._get_by_event_and_payment(event, payment_id)
        if existing is not None:
            return existing, False

        webhook_id = generate_id("webhook")
        received_at = self._clock.now().isoformat()
        try:
            self._conn.execute(
                """
                INSERT INTO webhooks (webhook_id, event, payment_id, order_id, raw_body, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (webhook_id, event, payment_id, order_id, raw_body, received_at),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent delivery of the same (event, payment_id).
            existing = self._get_by_event_and_payment(event, payment_id)
            assert existing is not None
            return existing, False

        return (
            WebhookRecord(
                webhook_id=webhook_id,
                event=event,
                payment_id=payment_id,
                order_id=order_id,
                raw_body=raw_body,
                received_at=received_at,
            ),
            True,
        )

    def get_for_order(self, order_id: str) -> list[WebhookRecord]:
        rows = self._conn.execute(
            "SELECT * FROM webhooks WHERE order_id = ? ORDER BY received_at ASC", (order_id,)
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _get_by_event_and_payment(self, event: str, payment_id: str) -> WebhookRecord | None:
        row = self._conn.execute(
            "SELECT * FROM webhooks WHERE event = ? AND payment_id = ?", (event, payment_id)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def _row_to_record(self, row: sqlite3.Row) -> WebhookRecord:
        record = dict(row)
        return WebhookRecord(
            webhook_id=record["webhook_id"],
            event=record["event"],
            payment_id=record["payment_id"],
            order_id=record["order_id"],
            raw_body=record["raw_body"],
            received_at=record["received_at"],
        )
