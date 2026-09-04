from agent_commerce.payments.idempotency import IdempotencyStore, idempotency_key
from agent_commerce.payments.models import OrderRecord, OrderStatus


def _order(order_id: str = "order_1") -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        transaction_id="txn_1",
        amount_paise=100000,
        currency="INR",
        receipt="txn_1:1",
        status=OrderStatus.CREATED,
        notes={"transaction_id": "txn_1", "policy_version": "abc123"},
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_idempotency_key_format() -> None:
    assert idempotency_key("txn_abc", 1) == "txn_abc:1"
    assert idempotency_key("txn_abc", 2) == "txn_abc:2"


def test_get_returns_none_when_absent(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idem.db")
    assert store.get("txn_1:1") is None


def test_put_then_get_round_trips(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idem.db")
    order = _order()
    store.put("txn_1:1", order)
    fetched = store.get("txn_1:1")
    assert fetched == order


def test_different_attempt_numbers_are_different_keys(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idem.db")
    order1 = _order("order_1")
    order2 = _order("order_2")
    store.put(idempotency_key("txn_1", 1), order1)
    store.put(idempotency_key("txn_1", 2), order2)
    assert store.get(idempotency_key("txn_1", 1)) == order1
    assert store.get(idempotency_key("txn_1", 2)) == order2


def test_persists_across_store_instances(tmp_path) -> None:
    order = _order()
    store1 = IdempotencyStore(tmp_path / "idem.db")
    store1.put("txn_1:1", order)

    store2 = IdempotencyStore(tmp_path / "idem.db")
    assert store2.get("txn_1:1") == order
