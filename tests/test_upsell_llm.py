import logging

import jsonschema
import pytest

from agent_commerce.agents.upsell.llm import _DECIDE_TOOL, LLMStrategy
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


def test_llm_call_raising_fails_closed_to_no_offer(make_catalog) -> None:
    # Observed live: Groq's server-side tool-call validation can reject a response that omits
    # a nullable field entirely (rather than sending it as null), raising before any response
    # object exists to parse. This class promises to fail closed on any malformed decision —
    # that promise must hold even when the failure happens before parsing gets a chance to run.
    class _RaisingLLM(FakeLLMClient):
        def complete(self, **kwargs):
            raise RuntimeError("simulated server-side tool-call validation failure")

    catalog = make_catalog(_PRODUCTS)
    strategy = LLMStrategy(_RaisingLLM([]), catalog)

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


def test_decide_tool_schema_accepts_no_offer_without_sku_or_discount() -> None:
    # Regression test for the bug where Groq's server-side validation rejected a genuine
    # no-offer decision because sku/discount_pct were listed as required even though a
    # decline naturally omits both entirely (rather than sending them as null).
    jsonschema.validate(
        instance={"offered": False, "reasoning": "No candidate meets the margin floor."},
        schema=_DECIDE_TOOL.input_schema,
    )


def test_decide_tool_schema_accepts_offer_with_sku_and_discount() -> None:
    jsonschema.validate(
        instance={
            "offered": True,
            "sku": "SKU-A002",
            "discount_pct": 10,
            "reasoning": "High margin complement.",
        },
        schema=_DECIDE_TOOL.input_schema,
    )


def test_decide_tool_schema_still_requires_offered_and_reasoning() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"reasoning": "x"}, schema=_DECIDE_TOOL.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"offered": False}, schema=_DECIDE_TOOL.input_schema)


def test_llm_call_raising_records_a_distinct_machine_reason(make_catalog) -> None:
    class _RaisingLLM(FakeLLMClient):
        def complete(self, **kwargs):
            raise RuntimeError("simulated server-side tool-call validation failure")

    catalog = make_catalog(_PRODUCTS)
    strategy = LLMStrategy(_RaisingLLM([]), catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert decision.machine_reason == "UPSELL_DECISION_CALL_FAILED"


def test_genuine_no_offer_decision_has_no_machine_reason(make_catalog) -> None:
    # machine_reason is None only for a genuine, successfully-parsed model decision — this
    # is what lets the ledger distinguish "model declined" from "call failed and we fell
    # back to decline" (see docs/PROGRESS.md).
    catalog = make_catalog(_PRODUCTS)
    llm = FakeLLMClient(
        [
            tool_response(
                "upsell_decision",
                {"offered": False, "reasoning": "No item meets the margin floor."},
            )
        ]
    )
    strategy = LLMStrategy(llm, catalog)

    decision = strategy.decide(_cart_with_a001(), _rules())

    assert isinstance(decision, NoOffer)
    assert decision.machine_reason is None


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
