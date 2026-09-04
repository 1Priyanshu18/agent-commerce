from agent_commerce.agents.upsell.none import NoneStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules, NoOffer
from agent_commerce.cart.models import Cart, CartItem


def test_none_strategy_never_offers_on_empty_cart() -> None:
    strategy = NoneStrategy()
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())
    decision = strategy.decide(Cart(transaction_id="txn_1"), rules)
    assert isinstance(decision, NoOffer)
    assert decision.reasoning


def test_none_strategy_never_offers_on_nonempty_cart() -> None:
    strategy = NoneStrategy()
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())
    cart = Cart(transaction_id="txn_1")
    cart.add(CartItem(sku="SKU-A001", name="x", unit_price_paise=1000, unit_cost_paise=500, quantity=1))
    decision = strategy.decide(cart, rules)
    assert isinstance(decision, NoOffer)
