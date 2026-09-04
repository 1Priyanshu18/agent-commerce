import pytest

from agent_commerce.agents.upsell.rules import RulesStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules, NoOffer, Offer
from agent_commerce.cart.models import Cart, CartItem

_PRODUCTS = [
    {
        "sku": "SKU-A001",
        "name": "Cart Item",
        "category": "Toys",
        "price_paise": 100000,
        "cost_paise": 70000,
        "stock": 10,
        "tags": ["building"],
    },
    {
        "sku": "SKU-A002",
        "name": "High Margin Toy",
        "category": "Toys",
        "price_paise": 50000,
        "cost_paise": 20000,
        "stock": 5,
        "tags": ["puzzle"],
    },
    {
        "sku": "SKU-A003",
        "name": "Low Margin Toy",
        "category": "Toys",
        "price_paise": 40000,
        "cost_paise": 32000,
        "stock": 5,
        "tags": ["building"],
    },
]


def _cart_with_a001() -> Cart:
    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(sku="SKU-A001", name="Cart Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1)
    )
    return cart


def test_picks_highest_margin_candidate_and_caps_at_policy_discount(make_catalog) -> None:
    # SKU-A002 margin = (50000-20000)/50000 = 60%, SKU-A003 margin = 20% -> A002 wins.
    # min_margin_pct=12 -> min_selling_price = 20000/0.88 = 22727.27 -> needed ~54.5%,
    # capped by max_discount_pct=15.
    catalog = make_catalog(_PRODUCTS)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())

    decision = strategy.decide(_cart_with_a001(), rules)

    assert isinstance(decision, Offer)
    assert decision.sku == "SKU-A002"
    assert decision.discount_pct == 15.0
    assert "SKU-A002" in decision.reasoning


def test_margin_floor_binds_before_policy_cap(make_catalog) -> None:
    # min_margin_pct=55 -> min_selling_price = 20000/0.45 = 44444.44 ->
    # needed = (1 - 44444.44/50000)*100 = 11.11%, which is below the 15% cap.
    catalog = make_catalog(_PRODUCTS)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=55, blacklist_skus=frozenset())

    decision = strategy.decide(_cart_with_a001(), rules)

    assert isinstance(decision, Offer)
    assert decision.sku == "SKU-A002"
    assert decision.discount_pct == pytest.approx(11.11, abs=0.01)


def test_no_offer_when_margin_floor_forbids_any_discount(make_catalog) -> None:
    products = [
        {
            "sku": "SKU-C001",
            "name": "Cart Item",
            "category": "Toys",
            "price_paise": 100000,
            "cost_paise": 70000,
            "stock": 10,
            "tags": ["x"],
        },
        # margin exactly 10% — below an 80% floor with zero room, min_selling_price > price
        {
            "sku": "SKU-C002",
            "name": "Thin Margin Toy",
            "category": "Toys",
            "price_paise": 50000,
            "cost_paise": 45000,
            "stock": 5,
            "tags": ["x"],
        },
    ]
    catalog = make_catalog(products)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=80, blacklist_skus=frozenset())

    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(sku="SKU-C001", name="Cart Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1)
    )

    decision = strategy.decide(cart, rules)

    assert isinstance(decision, NoOffer)
    assert "margin floor" in decision.reasoning


def test_no_offer_on_empty_cart(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())
    decision = strategy.decide(Cart(transaction_id="txn_1"), rules)
    assert isinstance(decision, NoOffer)


def test_no_offer_when_no_candidates(make_catalog) -> None:
    products = [
        {
            "sku": "SKU-D001",
            "name": "Lonely Item",
            "category": "Toys",
            "price_paise": 100000,
            "cost_paise": 70000,
            "stock": 10,
            "tags": [],
        },
    ]
    catalog = make_catalog(products)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())
    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(
            sku="SKU-D001", name="Lonely Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1
        )
    )
    decision = strategy.decide(cart, rules)
    assert isinstance(decision, NoOffer)


def test_is_deterministic_across_repeated_calls(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    strategy = RulesStrategy(catalog)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())
    cart = _cart_with_a001()

    first = strategy.decide(cart, rules)
    second = strategy.decide(cart, rules)

    assert first == second
