import pytest

from agent_commerce.cart.models import Cart, CartItem


def _widget(quantity: int) -> CartItem:
    return CartItem(
        sku="A", name="Widget", unit_price_paise=1000, unit_cost_paise=600, quantity=quantity
    )


def test_empty_cart_totals_are_zero() -> None:
    cart = Cart(transaction_id="txn_1")
    assert cart.subtotal_paise == 0
    assert cart.total_paise == 0
    assert cart.projected_margin_pct == 0.0


def test_add_and_totals() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(2))
    assert cart.subtotal_paise == 2000
    assert cart.total_paise == 2000
    # profit = (1000-600)*2 = 800; margin = 800/2000 = 40%
    assert cart.projected_margin_pct == 40.0


def test_adding_same_sku_twice_increments_quantity() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(1))
    cart.add(_widget(2))
    assert cart.items["A"].quantity == 3


def test_discount_reduces_total_and_margin() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(2))
    cart.discount_paise = 200
    assert cart.total_paise == 1800
    # profit = 800 - 200 = 600; margin = 600/1800 = 33.33%
    assert cart.projected_margin_pct == pytest.approx(33.33, abs=0.01)


def test_remove_partial_quantity() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(3))
    cart.remove("A", quantity=1)
    assert cart.items["A"].quantity == 2


def test_remove_all_deletes_line() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(1))
    cart.remove("A")
    assert "A" not in cart.items


def test_remove_unknown_sku_raises() -> None:
    cart = Cart(transaction_id="txn_1")
    with pytest.raises(KeyError):
        cart.remove("does-not-exist")


def test_to_view_is_json_serializable_shape() -> None:
    cart = Cart(transaction_id="txn_1")
    cart.add(_widget(1))
    view = cart.to_view()
    assert view["transaction_id"] == "txn_1"
    assert view["items"][0]["sku"] == "A"
    assert view["subtotal_paise"] == 1000
