import pytest

from agent_commerce.core.config import Config
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.payments import build_payment_stack
from agent_commerce.payments.models import OrderStatus


def _config(**overrides) -> Config:
    defaults = dict(
        app_env="test",
        log_level="INFO",
        llm_provider="gemini",
        gemini_api_key="",
        gemini_model="gemini-3.6-flash",
        groq_api_key="",
        groq_model="llama-3.3-70b-versatile",
        anthropic_api_key="",
        anthropic_model="claude-haiku-4-5-20251001",
        llm_max_calls_per_run=200,
        payment_mode="simulated",
        razorpay_key_id="",
        razorpay_key_secret="",
        razorpay_webhook_secret="test_secret",
        reconcile_poll_interval_seconds=30,
        data_dir="data",
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_simulated_stack_full_lifecycle_reaches_matched(tmp_path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.db")
    stack = build_payment_stack(_config(), ledger=ledger, data_dir=tmp_path)

    order = stack.adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")

    # The simulated adapter delivered its webhook synchronously during create_order(), which
    # triggered the reconciler via the on_new_webhook callback, which marked the order paid —
    # all of this wiring is what this test exists to prove actually connects.
    stored = stack.order_store.get(order.order_id)
    assert stored is not None
    assert stored.status == OrderStatus.PAID


def test_recording_adapter_persists_order_before_reconciliation_needs_it(tmp_path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.db")
    stack = build_payment_stack(_config(), ledger=ledger, data_dir=tmp_path)

    order = stack.adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")

    # If the order weren't persisted before the webhook-triggered reconcile() ran, reconcile
    # would have hit the "no local order record" mismatch path instead.
    entries = ledger.entries_for_transaction("txn_1")
    reconciliation_entries = [e for e in entries if e.machine_reason == "RECONCILIATION_MATCHED"]
    assert len(reconciliation_entries) == 1
    assert order.order_id


def test_idempotent_retry_does_not_duplicate_order_or_webhook(tmp_path) -> None:
    ledger = LedgerStore(tmp_path / "ledger.db")
    stack = build_payment_stack(_config(), ledger=ledger, data_dir=tmp_path)

    first = stack.adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")
    second = stack.adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")

    assert first.order_id == second.order_id
    webhooks = stack.reconciler._webhook_store.get_for_order(first.order_id)
    assert len(webhooks) == 1


def test_unknown_payment_mode_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown PAYMENT_MODE"):
        build_payment_stack(
            _config(payment_mode="bogus"), ledger=LedgerStore(":memory:"), data_dir=tmp_path
        )
