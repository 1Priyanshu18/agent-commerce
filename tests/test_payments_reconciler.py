from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments.models import (
    OrderRecord,
    OrderStatus,
    PaymentRecord,
    PaymentStatus,
    ReconciliationStatus,
)
from agent_commerce.payments.order_store import OrderStore
from agent_commerce.payments.reconciler import Reconciler
from agent_commerce.payments.webhook_store import WebhookStore


class _FakeAdapter:
    """A fake satisfying just the fetch_payments() side of PaymentAdapter — the reconciler
    only ever calls this, never create_order(), and never branches on which adapter it is.
    """

    def __init__(self, payments_by_order: dict[str, list[PaymentRecord]]) -> None:
        self._payments_by_order = payments_by_order

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        return self._payments_by_order.get(order_id, [])


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


def _captured_payment(order_id: str = "order_1") -> PaymentRecord:
    return PaymentRecord(
        payment_id="pay_1",
        order_id=order_id,
        amount_paise=100000,
        currency="INR",
        status=PaymentStatus.CAPTURED,
        method="card",
        captured=True,
        error_code=None,
        error_description=None,
    )


def _stack(tmp_path, payments_by_order: dict[str, list[PaymentRecord]]):
    order_store = OrderStore(tmp_path / "orders.db")
    webhook_store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    adapter = _FakeAdapter(payments_by_order)
    reconciler = Reconciler(
        adapter=adapter, order_store=order_store, webhook_store=webhook_store, ledger=ledger
    )
    return reconciler, order_store, webhook_store, ledger


def test_unknown_order_is_a_mismatch(tmp_path) -> None:
    reconciler, _, _, _ = _stack(tmp_path, {})
    result = reconciler.reconcile("order_does_not_exist")
    assert result.status == ReconciliationStatus.MISMATCH


def test_no_payment_no_webhook_is_pending(tmp_path) -> None:
    reconciler, order_store, _, _ = _stack(tmp_path, {"order_1": []})
    order_store.save(_order())
    result = reconciler.reconcile("order_1")
    assert result.status == ReconciliationStatus.PENDING


def test_captured_payment_and_matching_webhook_is_matched(tmp_path) -> None:
    reconciler, order_store, webhook_store, _ = _stack(tmp_path, {"order_1": [_captured_payment()]})
    order_store.save(_order())
    webhook_store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")

    result = reconciler.reconcile("order_1")

    assert result.status == ReconciliationStatus.MATCHED
    assert order_store.get("order_1").status == OrderStatus.PAID


def test_order_paid_event_also_counts_as_captured_confirmation(tmp_path) -> None:
    reconciler, order_store, webhook_store, _ = _stack(tmp_path, {"order_1": [_captured_payment()]})
    order_store.save(_order())
    webhook_store.save_if_new(event="order.paid", payment_id="pay_1", order_id="order_1", raw_body="{}")

    result = reconciler.reconcile("order_1")

    assert result.status == ReconciliationStatus.MATCHED


def test_captured_payment_without_webhook_is_mismatch(tmp_path) -> None:
    # A dropped webhook: the payment happened (fresh GET confirms it) but we never heard
    # about it. This is exactly the scenario the polling reconciler exists to catch.
    reconciler, order_store, _, _ = _stack(tmp_path, {"order_1": [_captured_payment()]})
    order_store.save(_order())

    result = reconciler.reconcile("order_1")

    assert result.status == ReconciliationStatus.MISMATCH


def test_webhook_without_captured_payment_is_mismatch(tmp_path) -> None:
    # A webhook claims capture, but a fresh GET disagrees — worth flagging loudly.
    reconciler, order_store, webhook_store, _ = _stack(tmp_path, {"order_1": []})
    order_store.save(_order())
    webhook_store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")

    result = reconciler.reconcile("order_1")

    assert result.status == ReconciliationStatus.MISMATCH


def test_non_captured_payment_with_no_webhook_stays_pending(tmp_path) -> None:
    authorized = PaymentRecord(
        payment_id="pay_1",
        order_id="order_1",
        amount_paise=100000,
        currency="INR",
        status=PaymentStatus.AUTHORIZED,
        method="card",
        captured=False,
        error_code=None,
        error_description=None,
    )
    reconciler, order_store, _, _ = _stack(tmp_path, {"order_1": [authorized]})
    order_store.save(_order())

    result = reconciler.reconcile("order_1")

    assert result.status == ReconciliationStatus.PENDING


def test_reconcile_writes_ledger_entry_with_human_reason(tmp_path) -> None:
    reconciler, order_store, webhook_store, ledger = _stack(tmp_path, {"order_1": [_captured_payment()]})
    order_store.save(_order())
    webhook_store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")

    reconciler.reconcile("order_1")

    entries = ledger.entries_for_transaction("txn_1")
    assert len(entries) == 1
    assert entries[0].human_reason
    assert entries[0].machine_reason == "RECONCILIATION_MATCHED"


def test_reconcile_all_pending_covers_every_non_paid_order(tmp_path) -> None:
    reconciler, order_store, webhook_store, _ = _stack(
        tmp_path, {"order_1": [_captured_payment("order_1")], "order_2": []}
    )
    order_store.save(_order("order_1"))
    order_store.save(
        OrderRecord(
            order_id="order_2",
            transaction_id="txn_2",
            amount_paise=50000,
            currency="INR",
            receipt="txn_2:1",
            status=OrderStatus.CREATED,
            notes={"transaction_id": "txn_2", "policy_version": "abc123"},
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    webhook_store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")

    results = reconciler.reconcile_all_pending()

    statuses = {r.order_id: r.status for r in results}
    assert statuses["order_1"] == ReconciliationStatus.MATCHED
    assert statuses["order_2"] == ReconciliationStatus.PENDING


def test_reconcile_all_pending_excludes_already_paid_orders(tmp_path) -> None:
    reconciler, order_store, _, _ = _stack(tmp_path, {"order_1": []})
    order_store.save(_order("order_1"))
    order_store.update_status("order_1", OrderStatus.PAID)

    results = reconciler.reconcile_all_pending()

    assert results == []
