from agent_commerce.payments.models import OrderRecord, OrderStatus
from agent_commerce.payments.order_store import OrderStore


def _order(
    order_id: str = "order_1", transaction_id: str = "txn_1", status: OrderStatus = OrderStatus.CREATED
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        transaction_id=transaction_id,
        amount_paise=100000,
        currency="INR",
        receipt=f"{transaction_id}:1",
        status=status,
        notes={"transaction_id": transaction_id, "policy_version": "abc123"},
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_save_then_get(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    order = _order()
    store.save(order)
    assert store.get("order_1") == order


def test_get_unknown_returns_none(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    assert store.get("order_does_not_exist") is None


def test_get_by_transaction(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    order = _order()
    store.save(order)
    assert store.get_by_transaction("txn_1") == order


def test_get_by_transaction_returns_most_recent(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    store.save(_order("order_1", "txn_1"))
    store.save(_order("order_2", "txn_1"))
    result = store.get_by_transaction("txn_1")
    assert result.order_id == "order_2"


def test_update_status(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    store.save(_order())
    store.update_status("order_1", OrderStatus.PAID)
    assert store.get("order_1").status == OrderStatus.PAID


def test_all_pending_excludes_paid_orders(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    store.save(_order("order_1", "txn_1", OrderStatus.CREATED))
    store.save(_order("order_2", "txn_2", OrderStatus.PAID))
    pending = store.all_pending()
    assert {o.order_id for o in pending} == {"order_1"}


def test_save_is_idempotent_on_order_id_updating_status(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    store.save(_order("order_1", "txn_1", OrderStatus.CREATED))
    store.save(_order("order_1", "txn_1", OrderStatus.PAID))
    assert store.get("order_1").status == OrderStatus.PAID


def test_save_if_absent_inserts_when_missing(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.db")
    store.save_if_absent(_order())
    assert store.get("order_1") is not None


def test_save_if_absent_does_not_clobber_an_existing_rows_status(tmp_path) -> None:
    # This is the exact scenario RecordingPaymentAdapter hits with the simulated adapter:
    # the order is saved as CREATED, something else (the reconciler) bumps it to PAID, and
    # then a stale CREATED-status OrderRecord object gets passed to save_if_absent() again.
    store = OrderStore(tmp_path / "orders.db")
    store.save(_order("order_1", "txn_1", OrderStatus.CREATED))
    store.update_status("order_1", OrderStatus.PAID)

    stale_order = _order("order_1", "txn_1", OrderStatus.CREATED)
    store.save_if_absent(stale_order)

    assert store.get("order_1").status == OrderStatus.PAID
