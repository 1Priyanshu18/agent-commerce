from agent_commerce.payments.webhook_store import WebhookStore


def test_save_if_new_first_time_is_new(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    record, is_new = store.save_if_new(
        event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}"
    )
    assert is_new is True
    assert record.event == "payment.captured"
    assert record.payment_id == "pay_1"


def test_save_if_new_duplicate_event_and_payment_returns_existing(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    first, is_new_1 = store.save_if_new(
        event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}"
    )
    second, is_new_2 = store.save_if_new(
        event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body='{"different": true}'
    )
    assert is_new_1 is True
    assert is_new_2 is False
    assert second.webhook_id == first.webhook_id
    assert second.raw_body == first.raw_body  # the original is kept, not overwritten


def test_same_payment_different_event_is_a_new_record(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    _, is_new_1 = store.save_if_new(
        event="payment.authorized", payment_id="pay_1", order_id="order_1", raw_body="{}"
    )
    _, is_new_2 = store.save_if_new(
        event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}"
    )
    assert is_new_1 is True
    assert is_new_2 is True


def test_get_for_order_returns_only_that_orders_webhooks(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")
    store.save_if_new(event="payment.captured", payment_id="pay_2", order_id="order_2", raw_body="{}")
    records = store.get_for_order("order_1")
    assert len(records) == 1
    assert records[0].payment_id == "pay_1"


def test_get_for_order_unknown_order_returns_empty(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    assert store.get_for_order("order_does_not_exist") == []


def test_out_of_order_delivery_both_recorded(tmp_path) -> None:
    # A later event for the same payment arriving before an earlier one is normal; both are
    # distinct (event, payment_id) pairs and both get recorded.
    store = WebhookStore(tmp_path / "webhooks.db")
    store.save_if_new(event="payment.captured", payment_id="pay_1", order_id="order_1", raw_body="{}")
    store.save_if_new(event="payment.authorized", payment_id="pay_1", order_id="order_1", raw_body="{}")
    records = store.get_for_order("order_1")
    assert {r.event for r in records} == {"payment.captured", "payment.authorized"}
