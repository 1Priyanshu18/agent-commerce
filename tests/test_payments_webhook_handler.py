import hashlib
import hmac
import json

import pytest

from agent_commerce.ledger.models import ActionType
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments.webhook import WebhookHandler
from agent_commerce.payments.webhook_store import WebhookStore

SECRET = "test_webhook_secret"


def _body(*, event: str = "payment.captured", payment_id: str = "pay_1", order_id: str = "order_1") -> bytes:
    payload = {
        "entity": "event",
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 100000,
                    "status": "captured",
                    "notes": {"transaction_id": "txn_1", "policy_version": "abc123"},
                }
            }
        },
    }
    return json.dumps(payload).encode("utf-8")


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _handler(tmp_path):
    store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    # Explicit no-op: these tests exercise signature verification and storage, not
    # reconciliation — an intentional opt-out, not an accidental omission.
    handler = WebhookHandler(
        webhook_secret=SECRET, store=store, ledger=ledger, on_new_webhook=lambda order_id: None
    )
    return handler, store, ledger


def test_valid_signature_is_accepted(tmp_path) -> None:
    handler, _, _ = _handler(tmp_path)
    body = _body()
    result = handler.handle(raw_body=body, signature=_sign(body))
    assert result.accepted is True
    assert result.is_new is True


def test_invalid_signature_is_rejected(tmp_path) -> None:
    handler, _, ledger = _handler(tmp_path)
    body = _body()
    result = handler.handle(raw_body=body, signature="not-the-right-signature")
    assert result.accepted is False
    assert result.reason == "invalid signature"

    entries = ledger.all_entries()
    assert len(entries) == 1
    assert entries[0].machine_reason == "INVALID_WEBHOOK_SIGNATURE"


def test_signature_computed_with_wrong_secret_is_rejected(tmp_path) -> None:
    handler, _, _ = _handler(tmp_path)
    body = _body()
    wrong_signature = _sign(body, secret="wrong_secret")
    result = handler.handle(raw_body=body, signature=wrong_signature)
    assert result.accepted is False


def test_tampered_body_after_signing_is_rejected(tmp_path) -> None:
    handler, _, _ = _handler(tmp_path)
    body = _body()
    signature = _sign(body)
    tampered = _body(payment_id="pay_TAMPERED")
    result = handler.handle(raw_body=tampered, signature=signature)
    assert result.accepted is False


def test_valid_webhook_is_stored_and_logged_to_ledger(tmp_path) -> None:
    handler, store, ledger = _handler(tmp_path)
    body = _body()
    handler.handle(raw_body=body, signature=_sign(body))

    stored = store.get_for_order("order_1")
    assert len(stored) == 1
    assert stored[0].payment_id == "pay_1"

    entries = ledger.entries_for_transaction("txn_1")
    webhook_entries = [e for e in entries if e.action_type == ActionType.WEBHOOK]
    assert len(webhook_entries) == 1


def test_duplicate_valid_webhook_is_idempotent(tmp_path) -> None:
    handler, store, _ = _handler(tmp_path)
    body = _body()
    signature = _sign(body)
    r1 = handler.handle(raw_body=body, signature=signature)
    r2 = handler.handle(raw_body=body, signature=signature)
    assert r1.is_new is True
    assert r2.is_new is False
    assert len(store.get_for_order("order_1")) == 1


def test_on_new_webhook_callback_fires_only_for_new_webhooks(tmp_path) -> None:
    store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    calls = []
    handler = WebhookHandler(
        webhook_secret=SECRET,
        store=store,
        ledger=ledger,
        on_new_webhook=lambda order_id: calls.append(order_id),
    )
    body = _body()
    signature = _sign(body)
    handler.handle(raw_body=body, signature=signature)
    handler.handle(raw_body=body, signature=signature)  # duplicate

    assert calls == ["order_1"]


def test_missing_event_field_is_rejected(tmp_path) -> None:
    handler, _, _ = _handler(tmp_path)
    body = json.dumps({"payload": {"payment": {"entity": {"id": "pay_1", "order_id": "order_1"}}}}).encode()
    result = handler.handle(raw_body=body, signature=_sign(body))
    assert result.accepted is False
    assert result.reason == "missing event or payment id"


def test_new_webhook_with_no_callback_wired_raises_instead_of_silently_dropping(tmp_path) -> None:
    # A webhook that should trigger reconciliation but can't (no on_new_webhook wired) must
    # fail loudly — silently dropping it means an order can sit at "created" forever with no
    # signal anything is wrong.
    from agent_commerce.payments.webhook import UnwiredReconciliationCallbackError

    store = WebhookStore(tmp_path / "webhooks.db")
    ledger = LedgerStore(tmp_path / "ledger.db")
    handler = WebhookHandler(webhook_secret=SECRET, store=store, ledger=ledger)  # no on_new_webhook
    body = _body()

    with pytest.raises(UnwiredReconciliationCallbackError, match="order_1"):
        handler.handle(raw_body=body, signature=_sign(body))

    # The webhook itself must still be durably stored — the loud failure is about
    # reconciliation never being triggered, not about losing the audit record.
    assert len(store.get_for_order("order_1")) == 1
