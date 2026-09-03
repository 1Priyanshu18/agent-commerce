from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from agent_commerce.core.clock import Clock, SystemClock
from agent_commerce.core.ids import generate_id
from agent_commerce.core.json_canonical import canonical_json

from .models import ActionType, Actor, LedgerEntry, PolicyVerdict

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    transaction_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    caused_by TEXT NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT NOT NULL,
    reasoning_summary TEXT,
    machine_reason TEXT,
    human_reason TEXT,
    policy_verdict TEXT,
    policy_version TEXT,
    resulting_state TEXT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries is append-only: DELETE is not allowed');
END;
"""


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    entries_checked: int
    error: str | None = None


def _hash_payload(
    *,
    entry_id: str,
    transaction_id: str,
    timestamp: str,
    caused_by: list[str],
    actor: str,
    action_type: str,
    input: dict,
    output: dict,
    reasoning_summary: str | None,
    machine_reason: str | None,
    human_reason: str | None,
    policy_verdict: str | None,
    policy_version: str | None,
    resulting_state: dict | None,
) -> dict:
    return {
        "entry_id": entry_id,
        "transaction_id": transaction_id,
        "timestamp": timestamp,
        "caused_by": caused_by,
        "actor": actor,
        "action_type": action_type,
        "input": input,
        "output": output,
        "reasoning_summary": reasoning_summary,
        "machine_reason": machine_reason,
        "human_reason": human_reason,
        "policy_verdict": policy_verdict,
        "policy_version": policy_version,
        "resulting_state": resulting_state,
    }


class LedgerStore:
    """Append-only, hash-chained audit ledger.

    Append-only is enforced at the DB layer (triggers reject UPDATE/DELETE), not just by
    convention in this class. entry_hash = sha256(prev_hash + canonical_json(payload)); the
    chain is a single global sequence (ordered by seq), so any insertion, edit, or reordering
    anywhere in the ledger breaks verify_chain() for the whole store, not just one transaction.
    """

    def __init__(self, db_path: str | Path, clock: Clock | None = None) -> None:
        path = Path(db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()

    def append(
        self,
        *,
        transaction_id: str,
        caused_by: list[str],
        actor: Actor,
        action_type: ActionType,
        input: dict,
        output: dict,
        reasoning_summary: str | None = None,
        machine_reason: str | None = None,
        human_reason: str | None = None,
        policy_verdict: PolicyVerdict | None = None,
        policy_version: str | None = None,
        resulting_state: dict | None = None,
    ) -> LedgerEntry:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_hash FROM ledger_entries ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row else GENESIS_HASH

            entry_id = generate_id("entry")
            timestamp = self._clock.now().isoformat()
            actor_value = actor.value
            action_type_value = action_type.value
            policy_verdict_value = policy_verdict.value if policy_verdict is not None else None

            payload = _hash_payload(
                entry_id=entry_id,
                transaction_id=transaction_id,
                timestamp=timestamp,
                caused_by=caused_by,
                actor=actor_value,
                action_type=action_type_value,
                input=input,
                output=output,
                reasoning_summary=reasoning_summary,
                machine_reason=machine_reason,
                human_reason=human_reason,
                policy_verdict=policy_verdict_value,
                policy_version=policy_version,
                resulting_state=resulting_state,
            )
            entry_hash = hashlib.sha256(
                (prev_hash + canonical_json(payload)).encode("utf-8")
            ).hexdigest()

            cursor = self._conn.execute(
                """
                INSERT INTO ledger_entries (
                    entry_id, transaction_id, timestamp, caused_by, actor, action_type,
                    input, output, reasoning_summary, machine_reason, human_reason,
                    policy_verdict, policy_version, resulting_state, prev_hash, entry_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    transaction_id,
                    timestamp,
                    canonical_json(caused_by),
                    actor_value,
                    action_type_value,
                    canonical_json(input),
                    canonical_json(output),
                    reasoning_summary,
                    machine_reason,
                    human_reason,
                    policy_verdict_value,
                    policy_version,
                    canonical_json(resulting_state) if resulting_state is not None else None,
                    prev_hash,
                    entry_hash,
                ),
            )
            self._conn.commit()

            return LedgerEntry(
                seq=cursor.lastrowid,
                entry_id=entry_id,
                transaction_id=transaction_id,
                timestamp=timestamp,
                caused_by=caused_by,
                actor=actor,
                action_type=action_type,
                input=input,
                output=output,
                reasoning_summary=reasoning_summary,
                machine_reason=machine_reason,
                human_reason=human_reason,
                policy_verdict=policy_verdict,
                policy_version=policy_version,
                resulting_state=resulting_state,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )

    def last_entry_id(self, transaction_id: str) -> str | None:
        """Most recent entry for a transaction, for auto-chaining provenance (caused_by)
        without callers having to track entry ids themselves.
        """
        row = self._conn.execute(
            "SELECT entry_id FROM ledger_entries WHERE transaction_id = ? ORDER BY seq DESC LIMIT 1",
            (transaction_id,),
        ).fetchone()
        return row["entry_id"] if row else None

    def get(self, entry_id: str) -> LedgerEntry | None:
        row = self._conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def entries_for_transaction(self, transaction_id: str) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_entries WHERE transaction_id = ? ORDER BY seq ASC",
            (transaction_id,),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def all_entries(self) -> list[LedgerEntry]:
        rows = self._conn.execute("SELECT * FROM ledger_entries ORDER BY seq ASC").fetchall()
        return [self._row_to_entry(row) for row in rows]

    def verify_chain(self) -> ChainVerification:
        rows = self._conn.execute("SELECT * FROM ledger_entries ORDER BY seq ASC").fetchall()
        expected_prev = GENESIS_HASH
        for i, row in enumerate(rows):
            record = dict(row)

            if record["prev_hash"] != expected_prev:
                return ChainVerification(
                    False,
                    i,
                    f"entry {record['entry_id']} (seq {record['seq']}) has prev_hash "
                    f"{record['prev_hash']} but expected {expected_prev}",
                )

            payload = _hash_payload(
                entry_id=record["entry_id"],
                transaction_id=record["transaction_id"],
                timestamp=record["timestamp"],
                caused_by=json.loads(record["caused_by"]),
                actor=record["actor"],
                action_type=record["action_type"],
                input=json.loads(record["input"]),
                output=json.loads(record["output"]),
                reasoning_summary=record["reasoning_summary"],
                machine_reason=record["machine_reason"],
                human_reason=record["human_reason"],
                policy_verdict=record["policy_verdict"],
                policy_version=record["policy_version"],
                resulting_state=(
                    json.loads(record["resulting_state"])
                    if record["resulting_state"] is not None
                    else None
                ),
            )
            recomputed = hashlib.sha256(
                (record["prev_hash"] + canonical_json(payload)).encode("utf-8")
            ).hexdigest()
            if recomputed != record["entry_hash"]:
                return ChainVerification(
                    False,
                    i,
                    f"entry {record['entry_id']} (seq {record['seq']}) hash mismatch: "
                    f"stored {record['entry_hash']} recomputed {recomputed}",
                )
            expected_prev = record["entry_hash"]

        return ChainVerification(True, len(rows), None)

    def _row_to_entry(self, row: sqlite3.Row) -> LedgerEntry:
        record = dict(row)
        return LedgerEntry(
            seq=record["seq"],
            entry_id=record["entry_id"],
            transaction_id=record["transaction_id"],
            timestamp=record["timestamp"],
            caused_by=json.loads(record["caused_by"]),
            actor=Actor(record["actor"]),
            action_type=ActionType(record["action_type"]),
            input=json.loads(record["input"]),
            output=json.loads(record["output"]),
            reasoning_summary=record["reasoning_summary"],
            machine_reason=record["machine_reason"],
            human_reason=record["human_reason"],
            policy_verdict=(
                PolicyVerdict(record["policy_verdict"]) if record["policy_verdict"] else None
            ),
            policy_version=record["policy_version"],
            resulting_state=(
                json.loads(record["resulting_state"])
                if record["resulting_state"] is not None
                else None
            ),
            prev_hash=record["prev_hash"],
            entry_hash=record["entry_hash"],
        )

    def close(self) -> None:
        self._conn.close()
