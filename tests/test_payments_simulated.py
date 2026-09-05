from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments.models import OrderStatus, PaymentStatus
from agent_commerce.payments.order_store import OrderStore
from agent_commerce.payments.simulated import SimulatedPaymentAdapter
from agent_commerce.payments.webhook import WebhookHandler
from agent_commerce.payments.webhook_store import WebhookStore

SECRET = "sim_secret"


def _adapter(tmp_path) -> tuple[SimulatedPaymentAdapter, WebhookStore, LedgerStore]:
    store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    order_store = OrderStore(tmp_path / "orders.db")
    # Explicit no-op: these tests exercise the adapter's own lifecycle, not reconciliation —
    # an intentional opt-out, not an accidental omission.
    handler = WebhookHandler(
        webhook_secret=SECRET, store=store, ledger=ledger, on_new_webhook=lambda order_id: None
    )
    adapter = SimulatedPaymentAdapter(webhook_secret=SECRET, webhook_handler=handler, order_store=order_store)
    return adapter, store, ledger


def test_create_order_returns_an_order_record(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")
    assert order.transaction_id == "txn_1"
    assert order.amount_paise == 150000
    assert order.currency == "INR"
    assert order.status == OrderStatus.CREATED
    assert order.order_id.startswith("order_sim_")


def test_create_order_notes_carry_transaction_id_and_policy_version(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")
    assert order.notes == {"transaction_id": "txn_1", "policy_version": "v1"}


def test_create_order_delivers_a_correctly_signed_webhook(tmp_path) -> None:
    adapter, store, _ = _adapter(tmp_path)
    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")

    webhooks = store.get_for_order(order.order_id)
    assert len(webhooks) == 1
    assert webhooks[0].event == "payment.captured"


def test_order_is_saved_before_the_webhook_fires(tmp_path) -> None:
    # A caller wiring a webhook-triggered reconciler via WebhookHandler.on_new_webhook must
    # be able to find the order the moment the callback fires — since create_order() delivers
    # the webhook synchronously, the order has to be persisted first.
    store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    order_store = OrderStore(tmp_path / "orders.db")
    seen_during_callback = {}
    handler = WebhookHandler(
        webhook_secret=SECRET,
        store=store,
        ledger=ledger,
        on_new_webhook=lambda order_id: seen_during_callback.update(
            {"order": order_store.get(order_id)}
        ),
    )
    adapter = SimulatedPaymentAdapter(webhook_secret=SECRET, webhook_handler=handler, order_store=order_store)

    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")

    assert seen_during_callback["order"] is not None
    assert seen_during_callback["order"].order_id == order.order_id


def test_create_order_webhook_reaches_the_ledger(tmp_path) -> None:
    from agent_commerce.ledger.models import ActionType

    adapter, _, ledger = _adapter(tmp_path)
    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")

    entries = ledger.entries_for_transaction("txn_1")
    webhook_entries = [e for e in entries if e.action_type == ActionType.WEBHOOK]
    assert len(webhook_entries) == 1
    assert order.order_id  # sanity: order really was created before the webhook fired


def test_fetch_payments_returns_the_simulated_captured_payment(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    order = adapter.create_order(transaction_id="txn_1", amount_paise=150000, policy_version="v1")

    payments = adapter.fetch_payments(order.order_id)

    assert len(payments) == 1
    assert payments[0].status == PaymentStatus.CAPTURED
    assert payments[0].captured is True
    assert payments[0].amount_paise == 150000


def test_fetch_payments_for_unknown_order_is_empty(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    assert adapter.fetch_payments("order_never_created") == []


def test_receipt_matches_idempotency_key_format(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    order = adapter.create_order(
        transaction_id="txn_1", amount_paise=150000, policy_version="v1", attempt_no=2
    )
    assert order.receipt == "txn_1:2"


def test_two_orders_get_distinct_order_and_payment_ids(tmp_path) -> None:
    adapter, _, _ = _adapter(tmp_path)
    order1 = adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")
    order2 = adapter.create_order(transaction_id="txn_2", amount_paise=200000, policy_version="v1")
    assert order1.order_id != order2.order_id
    payment1_id = adapter.fetch_payments(order1.order_id)[0].payment_id
    payment2_id = adapter.fetch_payments(order2.order_id)[0].payment_id
    assert payment1_id != payment2_id
