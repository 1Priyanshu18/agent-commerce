from agent_commerce.payments.idempotency import IdempotencyStore
from agent_commerce.payments.idempotent_adapter import IdempotentPaymentAdapter
from agent_commerce.payments.models import OrderRecord, OrderStatus, PaymentRecord, PaymentStatus


class _CountingAdapter:
    def __init__(self) -> None:
        self.create_calls = 0
        self.fetch_calls = 0

    def create_order(
        self, *, transaction_id: str, amount_paise: int, policy_version: str, attempt_no: int = 1
    ):
        self.create_calls += 1
        return OrderRecord(
            order_id=f"order_{self.create_calls}",
            transaction_id=transaction_id,
            amount_paise=amount_paise,
            currency="INR",
            receipt=f"{transaction_id}:{attempt_no}",
            status=OrderStatus.CREATED,
            notes={"transaction_id": transaction_id, "policy_version": policy_version},
            created_at="2026-01-01T00:00:00+00:00",
        )

    def fetch_payments(self, order_id: str) -> list[PaymentRecord]:
        self.fetch_calls += 1
        return [
            PaymentRecord(
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
        ]


def test_first_call_creates_a_real_order(tmp_path) -> None:
    wrapped = _CountingAdapter()
    adapter = IdempotentPaymentAdapter(wrapped, IdempotencyStore(tmp_path / "idem.db"))

    order = adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")

    assert wrapped.create_calls == 1
    assert order.order_id == "order_1"


def test_retry_with_same_transaction_and_attempt_does_not_create_a_second_order(tmp_path) -> None:
    wrapped = _CountingAdapter()
    adapter = IdempotentPaymentAdapter(wrapped, IdempotencyStore(tmp_path / "idem.db"))

    first = adapter.create_order(
        transaction_id="txn_1", amount_paise=100000, policy_version="v1", attempt_no=1
    )
    second = adapter.create_order(
        transaction_id="txn_1", amount_paise=100000, policy_version="v1", attempt_no=1
    )

    assert wrapped.create_calls == 1  # the retry never reached the wrapped adapter
    assert first == second


def test_different_attempt_no_creates_a_new_order(tmp_path) -> None:
    wrapped = _CountingAdapter()
    adapter = IdempotentPaymentAdapter(wrapped, IdempotencyStore(tmp_path / "idem.db"))

    first = adapter.create_order(
        transaction_id="txn_1", amount_paise=100000, policy_version="v1", attempt_no=1
    )
    second = adapter.create_order(
        transaction_id="txn_1", amount_paise=100000, policy_version="v1", attempt_no=2
    )

    assert wrapped.create_calls == 2
    assert first.order_id != second.order_id


def test_different_transaction_creates_a_new_order(tmp_path) -> None:
    wrapped = _CountingAdapter()
    adapter = IdempotentPaymentAdapter(wrapped, IdempotencyStore(tmp_path / "idem.db"))

    adapter.create_order(transaction_id="txn_1", amount_paise=100000, policy_version="v1")
    adapter.create_order(transaction_id="txn_2", amount_paise=100000, policy_version="v1")

    assert wrapped.create_calls == 2


def test_fetch_payments_always_delegates_without_caching(tmp_path) -> None:
    wrapped = _CountingAdapter()
    adapter = IdempotentPaymentAdapter(wrapped, IdempotencyStore(tmp_path / "idem.db"))

    adapter.fetch_payments("order_1")
    adapter.fetch_payments("order_1")

    assert wrapped.fetch_calls == 2  # always a fresh call — no idempotency on reads
