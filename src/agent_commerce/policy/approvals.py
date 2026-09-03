"""Destination for REQUIRE_APPROVAL verdicts. Overdue pending approvals fail closed: expire()
flips them to 'timed_out', which callers must treat as a DENY, never as an implicit allow.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from agent_commerce.core.clock import Clock, SystemClock
from agent_commerce.core.ids import generate_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    ledger_entry_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'timed_out')),
    created_at TEXT NOT NULL,
    timeout_at TEXT NOT NULL,
    decided_at TEXT
);
"""


@dataclass(frozen=True)
class Approval:
    approval_id: str
    transaction_id: str
    ledger_entry_id: str
    tool_name: str
    arguments: dict
    status: str
    created_at: str
    timeout_at: str
    decided_at: str | None


class ApprovalStore:
    def __init__(self, db_path: str | Path, clock: Clock | None = None) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = clock or SystemClock()

    def create(
        self,
        *,
        transaction_id: str,
        ledger_entry_id: str,
        tool_name: str,
        arguments: dict,
        timeout_seconds: int,
    ) -> Approval:
        approval_id = generate_id("appr")
        now = self._clock.now()
        timeout_at = now + timedelta(seconds=timeout_seconds)
        self._conn.execute(
            """
            INSERT INTO approvals (
                approval_id, transaction_id, ledger_entry_id, tool_name, arguments,
                status, created_at, timeout_at, decided_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
            """,
            (
                approval_id,
                transaction_id,
                ledger_entry_id,
                tool_name,
                json.dumps(arguments),
                now.isoformat(),
                timeout_at.isoformat(),
            ),
        )
        self._conn.commit()
        approval = self.get(approval_id)
        assert approval is not None
        return approval

    def get(self, approval_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return self._row_to_approval(row) if row else None

    def list_pending(self) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def approve(self, approval_id: str) -> Approval:
        return self._decide(approval_id, "approved")

    def deny(self, approval_id: str) -> Approval:
        return self._decide(approval_id, "denied")

    def _decide(self, approval_id: str, status: str) -> Approval:
        existing = self.get(approval_id)
        if existing is None:
            raise ValueError(f"unknown approval_id: {approval_id}")
        if existing.status != "pending":
            raise ValueError(f"approval {approval_id} already resolved as '{existing.status}'")
        now = self._clock.now().isoformat()
        self._conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE approval_id = ?",
            (status, now, approval_id),
        )
        self._conn.commit()
        decided = self.get(approval_id)
        assert decided is not None
        return decided

    def expire_overdue(self) -> list[Approval]:
        """Fail-closed sweep: any pending approval past its timeout becomes 'timed_out'."""
        now = self._clock.now().isoformat()
        rows = self._conn.execute(
            "SELECT approval_id FROM approvals WHERE status = 'pending' AND timeout_at <= ?",
            (now,),
        ).fetchall()
        expired: list[Approval] = []
        for row in rows:
            self._conn.execute(
                "UPDATE approvals SET status = 'timed_out', decided_at = ? WHERE approval_id = ?",
                (now, row["approval_id"]),
            )
            resolved = self.get(row["approval_id"])
            assert resolved is not None
            expired.append(resolved)
        if expired:
            self._conn.commit()
        return expired

    def _row_to_approval(self, row: sqlite3.Row) -> Approval:
        record = dict(row)
        return Approval(
            approval_id=record["approval_id"],
            transaction_id=record["transaction_id"],
            ledger_entry_id=record["ledger_entry_id"],
            tool_name=record["tool_name"],
            arguments=json.loads(record["arguments"]),
            status=record["status"],
            created_at=record["created_at"],
            timeout_at=record["timeout_at"],
            decided_at=record["decided_at"],
        )

    def close(self) -> None:
        self._conn.close()
