import logging

from agent_commerce.agents.upsell.llm import LLMStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules, NoOffer, Offer
from agent_commerce.cart.models import Cart, CartItem
from agent_commerce.core.llm import FakeLLMClient, text_response, tool_response

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
]


def _cart_with_a001() -> Cart:
    cart = Cart(transaction_id="txn_1")
    cart.add(
        CartItem(sku="SKU-A001", name="Cart Item", unit_price_paise=100000, unit_cost_paise=70000, quantity=1)
    )
    return cart


def _rules(max_discount_pct: float = 15, min_margin_pct: float = 12) -> MerchantRules:
    return MerchantRules(
        max_discount_pct=max_discount_pct, min_margin_pct=min_margin_pct, blacklist_skus=frozenset()
    )


def test_offer_happy_path(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {
                    "offered": True,
                    "sku": "SKU-A002",
                    "discount_pct": 10,
                    "reasoning": "High margin complement, well within policy.",
                },
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, Offer)
    assert decision.sku == "SKU-A002"
    assert decision.discount_pct == 10.0


def test_no_offer_happy_path(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {
                    "offered": False,
                    "sku": None,
                    "discount_pct": None,
                    "reasoning": "No item meets the margin floor.",
                },
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert decision.reasoning == "No item meets the margin floor."


def test_no_candidates_short_circuits_without_calling_llm(make_catalog) -> None:
    products = [
        {
            "sku": "SKU-D001",
            "name": "Lonely",
            "category": "Toys",
            "price_paise": 1000,
            "cost_paise": 500,
            "stock": 10,
            "tags": [],
        }
    ]
    catalog = make_catalog(products)
    llm = FakeLLMClient([])  # would raise if called
    strategy = LLMStrategy(llm, catalog)
    cart = Cart(transaction_id="txn_1")
    cart.add(CartItem(sku="SKU-D001", name="Lonely", unit_price_paise=1000, unit_cost_paise=500, quantity=1))

    decision = strategy.decide(cart, _rules())

    assert isinstance(decision, NoOffer)
    assert llm.calls == []


def test_missing_tool_call_fails_closed_to_no_offer(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient([text_response("I'd rather not use the tool.")])
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert "parse failure" in decision.reasoning


def test_offered_true_missing_sku_fails_closed(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision", {"offered": True, "sku": None, "discount_pct": 10, "reasoning": "x"}
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert "parse failure" in decision.reasoning


def test_hallucinated_sku_not_in_candidates_fails_closed(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {"offered": True, "sku": "SKU-DOES-NOT-EXIST", "discount_pct": 10, "reasoning": "x"},
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert "not in the candidate list" in decision.reasoning


def test_discount_over_cap_is_clamped(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision", {"offered": True, "sku": "SKU-A002", "discount_pct": 90, "reasoning": "x"}
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules(max_discount_pct=15))

    assert isinstance(decision, Offer)
    assert decision.discount_pct == 15.0


def test_dark_pattern_reasoning_is_logged(make_catalog, caplog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {"offered": True, "sku": "SKU-A002", "discount_pct": 10, "reasoning": "Hurry, only 1 left!"},
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    with caplog.at_level(logging.WARNING):
        decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, Offer)  # the check flags, but does not block the decision
    assert any("dark pattern" in record.message for record in caplog.records)


def test_clean_reasoning_is_not_logged(make_catalog, caplog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {
                    "offered": True,
                    "sku": "SKU-A002",
                    "discount_pct": 10,
                    "reasoning": "Highest margin complement in stock.",
                },
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    with caplog.at_level(logging.WARNING):
        strategy.decide(_cart_with_a001(), _rules())

    assert caplog.records == []
