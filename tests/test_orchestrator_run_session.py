from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.agents.upsell.rules import RulesStrategy
from agent_commerce.agents.upsell.strategy import MerchantRules
from agent_commerce.core.config import Config
from agent_commerce.core.llm import FakeLLMClient, text_response, tool_response
from agent_commerce.ledger.models import ActionType
from agent_commerce.orchestrator.run_session import MAX_TOOL_LOOP_TURNS, BuyerSessionRunner
from agent_commerce.payments import build_payment_stack
from agent_commerce.payments.adapter import PaymentAdapter
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

pytestmark = pytest.mark.anyio

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


def _payment_adapter(mcp_stack: SimpleNamespace, tmp_path: Path) -> PaymentAdapter:
    # The real simulated adapter, composed the same way build_payment_stack() composes it for
    # the deployed Space — not a test-only fake. Fully completes the lifecycle synchronously
    # (order -> payment -> signed webhook -> our own handler -> reconciliation), with no
    # network or manual step involved.
    config = Config(
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
        razorpay_webhook_secret="test_webhook_secret",
        reconcile_poll_interval_seconds=30,
        pending_reconciliation_threshold_seconds=30,
        demo_passphrase="",
        demo_max_calls_per_session=20,
        demo_daily_call_budget=50,
        data_dir=str(tmp_path),
    )
    stack = build_payment_stack(config, ledger=mcp_stack.ledger, data_dir=tmp_path)
    return stack.adapter


def _runner(
    mcp_stack: SimpleNamespace,
    tmp_path: Path,
    scripted_responses: list,
    *,
    simulated_payment_adapter=None,
) -> BuyerSessionRunner:
    llm = FakeLLMClient(scripted_responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    payment = _payment_adapter(mcp_stack, tmp_path)
    return BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=payment,
        simulated_payment_adapter=simulated_payment_adapter,
    )


_CONSTRAINTS_RESPONSE = tool_response(
    "extract_buyer_constraints",
    {
        "budget_ceiling_paise": 200000,
        "soft_target_paise": None,
        "category": "Toys & Games",
        "recipient_context": "10-year-old nephew",
        "must_have": [],
        "deadline": None,
    },
)


async def test_happy_path_session_end_to_end(mcp_stack: SimpleNamespace, tmp_path: Path) -> None:
    txn = "txn_happy_1"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response(
            "catalog.search", {"transaction_id": txn, "category": "Toys & Games", "max_price_paise": 200000}
        ),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "order_created"
    assert result.turns_used == 3
    assert result.cart_view is not None
    assert result.cart_view["items"][0]["sku"] == "SKU-0001"
    assert result.order is not None
    assert result.order["order_id"].startswith("order_sim_")
    assert result.denial_reason is None

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    action_types = [e.action_type for e in entries]
    assert ActionType.SEARCH in action_types
    assert ActionType.SELECT in action_types
    assert ActionType.PAYMENT_CALL in action_types
    assert ActionType.WEBHOOK in action_types  # the simulated adapter delivered a real webhook
    assert mcp_stack.ledger.verify_chain().ok is True


async def test_agent_hallucinated_transaction_id_is_overridden_with_the_real_one(
    mcp_stack: SimpleNamespace, tmp_path: Path
) -> None:
    # A real LLM has no way to know the session's actual transaction_id unless told, and
    # nothing in the prompt states it — so it's expected to invent a plausible-looking one of
    # its own (this exact behavior was observed live). If the orchestrator didn't normalize
    # this, the real MCP tool calls would mutate a *different* session's cart than the one the
    # orchestrator's own policy/stock checks are inspecting — silently splitting state in two
    # and making every failure-injection check see an empty cart.
    txn = "txn_real_one"
    hallucinated = "txn_the_agent_made_up"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response(
            "catalog.search",
            {"transaction_id": hallucinated, "category": "Toys & Games", "max_price_paise": 200000},
        ),
        tool_response("cart.add", {"transaction_id": hallucinated, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": hallucinated}),
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "order_created"
    assert result.cart_view["items"][0]["sku"] == "SKU-0001"

    # The cart landed under the REAL transaction_id, not the hallucinated one.
    real_cart = mcp_stack.sessions.get(txn)
    assert real_cart is not None
    assert "SKU-0001" in real_cart.items
    hallucinated_cart = mcp_stack.sessions.get(hallucinated)
    assert hallucinated_cart is None or not hallucinated_cart.items


async def test_checkout_denied_when_cart_exceeds_hard_ceiling(
    mcp_stack: SimpleNamespace, tmp_path: Path
) -> None:
    txn = "txn_over_budget"
    tight_constraints = tool_response(
        "extract_buyer_constraints",
        {
            "budget_ceiling_paise": 500,  # far below any real product price
            "soft_target_paise": None,
            "category": "Toys & Games",
            "recipient_context": None,
            "must_have": [],
            "deadline": None,
        },
    )
    responses = [
        tight_constraints,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
        # A DENY is recoverable now (the loop keeps going), but nothing in the catalog fits a
        # Rs 5 budget — the agent gives up rather than retrying blindly.
        text_response("I can't find anything that fits this budget; giving up."),
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "Spend almost nothing")

    assert result.outcome == "policy_denied"
    assert result.denial_reason is not None
    assert "exceeds buyer budget ceiling" in result.denial_reason
    assert result.order is None

    # The cart mutation itself still happened (cart.add isn't gated by budget_ceiling), but
    # checkout was blocked before any payment call.
    payment_entries = [
        e for e in mcp_stack.ledger.entries_for_transaction(txn) if e.action_type == ActionType.PAYMENT_CALL
    ]
    assert payment_entries == []


async def test_cart_add_denied_for_blacklisted_sku(mcp_stack: SimpleNamespace, tmp_path: Path) -> None:
    txn = "txn_blacklist"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Sports & Outdoors"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0042", "quantity": 1}),
        # A DENY is recoverable now (the loop keeps going) — this agent just stops instead of
        # trying a different SKU.
        text_response("That item isn't available; stopping here."),
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "buy something")

    assert result.outcome == "policy_denied"
    assert "not available for agent-initiated purchase" in result.denial_reason

    cart = mcp_stack.sessions.get(txn)
    # cart.add was intercepted before the MCP tool ever ran, so the blacklisted item never
    # entered the cart at all.
    assert cart is None or "SKU-0042" not in cart.items


async def test_turn_limit_is_enforced(mcp_stack: SimpleNamespace, tmp_path: Path) -> None:
    txn = "txn_loop"
    # The agent just keeps searching forever and never adds anything or checks out.
    responses = [_CONSTRAINTS_RESPONSE] + [
        tool_response("catalog.search", {"transaction_id": txn, "category": "Books"})
        for _ in range(MAX_TOOL_LOOP_TURNS + 2)
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "buy a book, maybe")

    assert result.outcome == "turn_limit_reached"
    assert result.turns_used == MAX_TOOL_LOOP_TURNS


async def test_no_tool_calls_ends_session_as_no_purchase(mcp_stack: SimpleNamespace, tmp_path: Path) -> None:
    txn = "txn_nothing"
    responses = [_CONSTRAINTS_RESPONSE, text_response("I couldn't find anything suitable, sorry.")]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "buy something impossible")

    assert result.outcome == "no_purchase"
    assert result.turns_used == 1


async def test_upsell_offer_accepted_adds_a_second_item_at_a_discount(
    mcp_stack: SimpleNamespace, tmp_path: Path
) -> None:
    txn = "txn_upsell_accept"
    # A generous ceiling — RulesStrategy's chosen complement (SKU-0009, ~Rs 2,499 before its
    # discount) plus SKU-0001 (~Rs 899) must both fit, or checkout.confirm denies for exceeding
    # budget and the session needs recovery turns this test doesn't script.
    generous_constraints = tool_response(
        "extract_buyer_constraints",
        {
            "budget_ceiling_paise": 500000,
            "soft_target_paise": None,
            "category": "Toys & Games",
            "recipient_context": "10-year-old nephew",
            "must_have": [],
            "deadline": None,
        },
    )
    responses = [
        generous_constraints,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        # decide_on_offer's forced tool call, consumed synchronously inside cart.add's
        # handling, before the loop's next turn:
        tool_response(
            "respond_to_offer", {"decision": "ACCEPT", "counter_price_paise": None, "reason": "sure"}
        ),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    llm = FakeLLMClient(responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    payment = _payment_adapter(mcp_stack, tmp_path)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset({"SKU-0042"}))
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=payment,
        upsell_strategy=RulesStrategy(mcp_stack.catalog),
        merchant_rules=rules,
        merchant_mcp=mcp_stack.merchant_mcp,
    )

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "order_created"
    assert len(result.cart_view["items"]) == 2

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    offer_entries = [e for e in entries if e.action_type == ActionType.OFFER]
    assert len(offer_entries) == 1
    assert offer_entries[0].output["offered"] is True

    upsell_selects = [
        e
        for e in entries
        if e.action_type == ActionType.SELECT and e.input.get("source") == "upsell_accepted"
    ]
    assert len(upsell_selects) == 1
    assert mcp_stack.ledger.verify_chain().ok is True


async def test_upsell_none_strategy_makes_no_offer(mcp_stack: SimpleNamespace, tmp_path: Path) -> None:
    txn = "txn_upsell_none"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = _runner(mcp_stack, tmp_path, responses)

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "order_created"
    assert len(result.cart_view["items"]) == 1

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    assert not any(e.action_type == ActionType.OFFER for e in entries)


async def test_upsell_decide_on_offer_raising_fails_closed_instead_of_crashing_session(
    mcp_stack: SimpleNamespace, tmp_path: Path
) -> None:
    # Observed live: Groq's server-side tool-call validation can reject a response that omits
    # a nullable field entirely (rather than sending it as null), raising before any response
    # object exists to parse. A malformed side-decision like this must never crash the whole
    # buying session — it must fail closed exactly like an unparseable response would.
    class _RaisingOnSecondCallLLM(FakeLLMClient):
        def complete(self, **kwargs):
            if len(self.calls) == 3:  # the decide_on_offer call, after constraints/search/add
                self.calls.append(kwargs)
                raise RuntimeError("simulated server-side tool-call validation failure")
            return super().complete(**kwargs)

    txn = "txn_upsell_decide_raises"
    generous_constraints = tool_response(
        "extract_buyer_constraints",
        {
            "budget_ceiling_paise": 500000,
            "soft_target_paise": None,
            "category": "Toys & Games",
            "recipient_context": "10-year-old nephew",
            "must_have": [],
            "deadline": None,
        },
    )
    responses = [
        generous_constraints,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Toys & Games"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        # the raising call is intercepted before this is ever popped
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    llm = _RaisingOnSecondCallLLM(responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    payment = _payment_adapter(mcp_stack, tmp_path)
    rules = MerchantRules(max_discount_pct=15, min_margin_pct=12, blacklist_skus=frozenset({"SKU-0042"}))
    runner = BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        catalog=mcp_stack.catalog,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=payment,
        upsell_strategy=RulesStrategy(mcp_stack.catalog),
        merchant_rules=rules,
        merchant_mcp=mcp_stack.merchant_mcp,
    )

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "order_created"
    assert len(result.cart_view["items"]) == 1  # the upsell item was never applied

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    parse_failures = [e for e in entries if e.action_type == ActionType.PARSE_FAILURE]
    assert len(parse_failures) == 1
    assert parse_failures[0].machine_reason == "UPSELL_RESPONSE_PARSE_FAILURE"
    assert mcp_stack.ledger.verify_chain().ok is True
