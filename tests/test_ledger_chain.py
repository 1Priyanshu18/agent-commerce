import sqlite3

import pytest

from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore


@pytest.fixture
def ledger(tmp_path) -> LedgerStore:
    return LedgerStore(tmp_path / "ledger.db")


def test_first_entry_chains_from_genesis(ledger: LedgerStore) -> None:
    entry = ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SEARCH,
        input={"text": "gift"},
        output={"count": 3},
    )
    assert entry.prev_hash == "0" * 64
    assert entry.entry_hash != entry.prev_hash


def test_chain_links_and_verifies(ledger: LedgerStore) -> None:
    e1 = ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SEARCH,
        input={"text": "gift"},
        output={"count": 3},
    )
    e2 = ledger.append(
        transaction_id="txn_1",
        caused_by=[e1.entry_id],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SELECT,
        input={"sku": "SKU-0001"},
        output={"total_paise": 89900},
    )
    assert e2.prev_hash == e1.entry_hash

    result = ledger.verify_chain()
    assert result.ok is True
    assert result.entries_checked == 2


def test_verify_chain_empty_ledger_ok(ledger: LedgerStore) -> None:
    result = ledger.verify_chain()
    assert result.ok is True
    assert result.entries_checked == 0


def test_update_is_rejected_at_db_layer(ledger: LedgerStore) -> None:
    ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SEARCH,
        input={},
        output={},
    )
    with pytest.raises(sqlite3.DatabaseError):
        ledger._conn.execute("UPDATE ledger_entries SET human_reason = 'tampered' WHERE seq = 1")


def test_delete_is_rejected_at_db_layer(ledger: LedgerStore) -> None:
    ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SEARCH,
        input={},
        output={},
    )
    with pytest.raises(sqlite3.DatabaseError):
        ledger._conn.execute("DELETE FROM ledger_entries WHERE seq = 1")


def test_tampered_row_fails_verification(ledger: LedgerStore) -> None:
    ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.ORCHESTRATOR,
        action_type=ActionType.SEARCH,
        input={},
        output={},
    )
    # The append-only triggers block this even from a second connection (see the two tests
    # above), so to exercise verify_chain()'s independent tamper detection we drop them first
    # — modelling a more severe threat (e.g. direct file-level edits that bypass SQLite's own
    # enforcement entirely). The hash chain must still catch the corruption on its own.
    ledger._conn.execute("DROP TRIGGER ledger_entries_no_update")
    ledger._conn.execute("UPDATE ledger_entries SET human_reason = 'tampered' WHERE seq = 1")
    ledger._conn.commit()

    result = ledger.verify_chain()
    assert result.ok is False
    assert result.error is not None


def test_reasoning_and_reason_fields_round_trip(ledger: LedgerStore) -> None:
    entry = ledger.append(
        transaction_id="txn_1",
        caused_by=[],
        actor=Actor.POLICY_ENGINE,
        action_type=ActionType.POLICY_CHECK,
        input={"cart_total_paise": 240000},
        output={"verdict": "DENY"},
        reasoning_summary="Cart exceeds ceiling.",
        machine_reason="BUDGET_CEILING_EXCEEDED",
        human_reason="cart total ₹2,400.00 exceeds buyer budget ceiling ₹2,000.00",
    )
    fetched = ledger.get(entry.entry_id)
    assert fetched is not None
    assert fetched.machine_reason == "BUDGET_CEILING_EXCEEDED"
    assert fetched.human_reason.startswith("cart total")
    assert ledger.verify_chain().ok is True
