from agent_commerce.agents.upsell.harness import StrategyOutcome, run_comparison
from agent_commerce.agents.upsell.llm import LLMStrategy
from agent_commerce.agents.upsell.none import NoneStrategy
from agent_commerce.agents.upsell.rules import RulesStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules, NoOffer, Offer
from agent_commerce.cart.models import Cart, CartItem
from agent_commerce.core.llm import FakeLLMClient, tool_response

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


def _rules() -> MerchantRules:
    return MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset())


def test_all_three_strategies_run_against_the_same_cart(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {"offered": True, "sku": "SKU-A002", "discount_pct": 15, "reasoning": "Best complement."},
            )
        ]
    )
    strategies = {
        "none": NoneStrategy(),
        "rules": RulesStrategy(catalog),
        "llm": LLMStrategy(llm, catalog),
    }

    outcomes = run_comparison(strategies, _cart_with_a001(), _rules(), catalog)

    assert [o.strategy_name for o in outcomes] == ["none", "rules", "llm"]
    assert all(isinstance(o, StrategyOutcome) for o in outcomes)


def test_none_baseline_never_offers(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    outcomes = run_comparison({"none": NoneStrategy()}, _cart_with_a001(), _rules(), catalog)
    assert isinstance(outcomes[0].decision, NoOffer)
    assert outcomes[0].margin_pct_if_accepted is None


def test_rules_baseline_offer_improves_margin_over_baseline(make_catalog) -> None:
    # SKU-A002 has a 60% margin, well above the cart's 30% baseline margin, so accepting it
    # should raise the blended margin.
    catalog = make_catalog(_PRODUCTS)
    outcomes = run_comparison({"rules": RulesStrategy(catalog)}, _cart_with_a001(), _rules(), catalog)
    outcome = outcomes[0]
    assert isinstance(outcome.decision, Offer)
    assert outcome.margin_pct_if_accepted is not None
    assert outcome.margin_pct_if_accepted > outcome.baseline_margin_pct


def test_baseline_margin_is_identical_across_strategies_for_the_same_cart(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {"offered": False, "sku": None, "discount_pct": None, "reasoning": "pass"},
            )
        ]
    )
    strategies = {"none": NoneStrategy(), "rules": RulesStrategy(catalog), "llm": LLMStrategy(llm, catalog)}

    outcomes = run_comparison(strategies, _cart_with_a001(), _rules(), catalog)

    baseline_margins = {o.baseline_margin_pct for o in outcomes}
    assert len(baseline_margins) == 1  # same cart -> same baseline margin regardless of strategy


def test_offer_reasoning_is_never_empty(make_catalog) -> None:
    catalog = make_catalog(_PRODUCTS)
    outcomes = run_comparison(
        {"none": NoneStrategy(), "rules": RulesStrategy(catalog)}, _cart_with_a001(), _rules(), catalog
    )
    for outcome in outcomes:
        assert outcome.decision.reasoning.strip() != ""
