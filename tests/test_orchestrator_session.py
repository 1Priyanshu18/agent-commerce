from agent_commerce.orchestrator.session import SessionRegistry


def test_get_or_create_creates_empty_cart_first_time() -> None:
    registry = SessionRegistry()
    cart = registry.get_or_create("txn_1")
    assert cart.transaction_id == "txn_1"
    assert cart.items == {}


def test_get_or_create_returns_same_cart_on_repeat_calls() -> None:
    registry = SessionRegistry()
    first = registry.get_or_create("txn_1")
    second = registry.get_or_create("txn_1")
    assert first is second


def test_get_returns_none_for_unknown_transaction() -> None:
    registry = SessionRegistry()
    assert registry.get("txn_unknown") is None


def test_get_returns_cart_created_via_get_or_create() -> None:
    registry = SessionRegistry()
    created = registry.get_or_create("txn_1")
    assert registry.get("txn_1") is created


def test_different_transactions_get_different_carts() -> None:
    registry = SessionRegistry()
    a = registry.get_or_create("txn_a")
    b = registry.get_or_create("txn_b")
    assert a is not b
