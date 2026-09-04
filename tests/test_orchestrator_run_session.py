from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_commerce.agents.buyer.agent import BuyerAgent
from agent_commerce.core.llm import FakeLLMClient, text_response, tool_response
from agent_commerce.ledger.models import ActionType
from agent_commerce.orchestrator.run_session import BuyerSessionRunner
from agent_commerce.payments.stub import StubPaymentAdapter
from agent_commerce.policy.compiler import compile_policy
from agent_commerce.policy.engine import PolicyEngine
from agent_commerce.policy.service import PolicyService

pytestmark = pytest.mark.anyio

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "default.yaml"


def _runner(mcp_stack: SimpleNamespace, scripted_responses: list) -> BuyerSessionRunner:
    llm = FakeLLMClient(scripted_responses)
    agent = BuyerAgent(llm)
    engine = PolicyEngine(compile_policy(REPO_POLICY_PATH))
    policy = PolicyService(engine, mcp_stack.ledger)
    payment = StubPaymentAdapter()
    return BuyerSessionRunner(
        agent=agent,
        buyer_mcp=mcp_stack.buyer_mcp,
        sessions=mcp_stack.sessions,
        ledger=mcp_stack.ledger,
        policy=policy,
        payment=payment,
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


async def test_happy_path_session_end_to_end(mcp_stack: SimpleNamespace) -> None:
    txn = "txn_happy_1"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response(
            "catalog.search", {"transaction_id": txn, "category": "Toys & Games", "max_price_paise": 200000}
        ),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0001", "quantity": 1}),
        tool_response("checkout.confirm", {"transaction_id": txn}),
    ]
    runner = _runner(mcp_stack, responses)

    result = await runner.run(txn, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")

    assert result.outcome == "checked_out"
    assert result.turns_used == 3
    assert result.cart_view is not None
    assert result.cart_view["items"][0]["sku"] == "SKU-0001"
    assert result.payment is not None
    assert result.payment["status"] == "paid"
    assert result.denial_reason is None

    entries = mcp_stack.ledger.entries_for_transaction(txn)
    action_types = [e.action_type for e in entries]
    assert ActionType.SEARCH in action_types
    assert ActionType.SELECT in action_types
    assert ActionType.PAYMENT_CALL in action_types
    assert mcp_stack.ledger.verify_chain().ok is True


async def test_checkout_denied_when_cart_exceeds_hard_ceiling(mcp_stack: SimpleNamespace) -> None:
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
    ]
    runner = _runner(mcp_stack, responses)

    result = await runner.run(txn, "Spend almost nothing")

    assert result.outcome == "policy_denied"
    assert result.denial_reason is not None
    assert "exceeds buyer budget ceiling" in result.denial_reason
    assert result.payment is None

    # The cart mutation itself still happened (cart.add isn't gated by budget_ceiling), but
    # checkout was blocked before any payment call.
    payment_entries = [
        e for e in mcp_stack.ledger.entries_for_transaction(txn) if e.action_type == ActionType.PAYMENT_CALL
    ]
    assert payment_entries == []


async def test_cart_add_denied_for_blacklisted_sku(mcp_stack: SimpleNamespace) -> None:
    txn = "txn_blacklist"
    responses = [
        _CONSTRAINTS_RESPONSE,
        tool_response("catalog.search", {"transaction_id": txn, "category": "Sports & Outdoors"}),
        tool_response("cart.add", {"transaction_id": txn, "sku": "SKU-0042", "quantity": 1}),
    ]
    runner = _runner(mcp_stack, responses)

    result = await runner.run(txn, "buy something")

    assert result.outcome == "policy_denied"
    assert "not available for agent-initiated purchase" in result.denial_reason

    cart = mcp_stack.sessions.get(txn)
    # cart.add was intercepted before the MCP tool ever ran, so the blacklisted item never
    # entered the cart at all.
    assert cart is None or "SKU-0042" not in cart.items


async def test_turn_limit_is_enforced(mcp_stack: SimpleNamespace) -> None:
    txn = "txn_loop"
    # The agent just keeps searching forever and never adds anything or checks out.
    responses = [_CONSTRAINTS_RESPONSE] + [
        tool_response("catalog.search", {"transaction_id": txn, "category": "Books"}) for _ in range(10)
    ]
    runner = _runner(mcp_stack, responses)

    result = await runner.run(txn, "buy a book, maybe")

    assert result.outcome == "turn_limit_reached"
    assert result.turns_used == 8


async def test_no_tool_calls_ends_session_as_no_purchase(mcp_stack: SimpleNamespace) -> None:
    txn = "txn_nothing"
    responses = [_CONSTRAINTS_RESPONSE, text_response("I couldn't find anything suitable, sorry.")]
    runner = _runner(mcp_stack, responses)

    result = await runner.run(txn, "buy something impossible")

    assert result.outcome == "no_purchase"
    assert result.turns_used == 1
