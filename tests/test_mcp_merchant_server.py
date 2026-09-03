from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError, ToolError

from agent_commerce.ledger.models import ActionType, Actor

pytestmark = pytest.mark.anyio


async def test_cart_read_at_checkout_sees_cart_mutated_via_buyer_server(mcp_stack: SimpleNamespace) -> None:
    # Cross-server integration: both servers share the same SessionRegistry.
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 2}
    )
    result = await mcp_stack.merchant_mcp.call_tool("cart.read_at_checkout", {"transaction_id": "txn_1"})
    assert result.structured_content["items"][0]["sku"] == "SKU-0001"
    assert result.structured_content["items"][0]["quantity"] == 2


async def test_cart_read_at_checkout_does_not_write_ledger_entry(mcp_stack: SimpleNamespace) -> None:
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 1}
    )
    before = len(mcp_stack.ledger.entries_for_transaction("txn_1"))
    await mcp_stack.merchant_mcp.call_tool("cart.read_at_checkout", {"transaction_id": "txn_1"})
    after = len(mcp_stack.ledger.entries_for_transaction("txn_1"))
    assert after == before


async def test_upsell_make_offer_records_offer_with_mandatory_reasoning(mcp_stack: SimpleNamespace) -> None:
    result = await mcp_stack.merchant_mcp.call_tool(
        "upsell.make_offer",
        {
            "transaction_id": "txn_1",
            "sku": "SKU-0001",
            "discount_pct": 10,
            "reasoning": "High-margin complement to the cart, within discount cap.",
        },
    )
    assert result.structured_content["offered"] is True
    entry = mcp_stack.ledger.entries_for_transaction("txn_1")[-1]
    assert entry.action_type == ActionType.OFFER
    assert entry.actor == Actor.UPSELL_AGENT
    assert entry.reasoning_summary == "High-margin complement to the cart, within discount cap."


async def test_upsell_make_offer_requires_reasoning(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="reasoning is required"):
        await mcp_stack.merchant_mcp.call_tool(
            "upsell.make_offer",
            {"transaction_id": "txn_1", "sku": "SKU-0001", "discount_pct": 10, "reasoning": "   "},
        )


async def test_upsell_make_offer_unknown_sku_raises(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="unknown SKU"):
        await mcp_stack.merchant_mcp.call_tool(
            "upsell.make_offer",
            {"transaction_id": "txn_1", "sku": "SKU-9999", "discount_pct": 10, "reasoning": "x"},
        )


async def test_upsell_make_offer_rejects_out_of_range_discount(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match=r"discount_pct must be within \[0, 100\]"):
        await mcp_stack.merchant_mcp.call_tool(
            "upsell.make_offer",
            {"transaction_id": "txn_1", "sku": "SKU-0001", "discount_pct": 150, "reasoning": "x"},
        )


async def test_upsell_no_offer_records_decision_with_mandatory_reasoning(mcp_stack: SimpleNamespace) -> None:
    result = await mcp_stack.merchant_mcp.call_tool(
        "upsell.no_offer",
        {"transaction_id": "txn_1", "reasoning": "No complementary in-stock item meets the margin floor."},
    )
    assert result.structured_content["offered"] is False
    entry = mcp_stack.ledger.entries_for_transaction("txn_1")[-1]
    assert entry.action_type == ActionType.OFFER
    assert entry.output == {"offered": False}
    assert entry.reasoning_summary == "No complementary in-stock item meets the margin floor."


async def test_upsell_no_offer_requires_reasoning(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="reasoning is required"):
        await mcp_stack.merchant_mcp.call_tool(
            "upsell.no_offer", {"transaction_id": "txn_1", "reasoning": ""}
        )


async def test_merchant_server_does_not_expose_buyer_only_tools(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(NotFoundError):
        await mcp_stack.merchant_mcp.call_tool(
            "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 1}
        )
    with pytest.raises(NotFoundError):
        await mcp_stack.merchant_mcp.call_tool("checkout.confirm", {"transaction_id": "txn_1"})
    with pytest.raises(NotFoundError):
        await mcp_stack.merchant_mcp.call_tool("catalog.search", {"transaction_id": "txn_1"})


async def test_merchant_server_has_no_cart_mutation_tool_registered(mcp_stack: SimpleNamespace) -> None:
    tools = await mcp_stack.merchant_mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {"cart.read_at_checkout", "upsell.make_offer", "upsell.no_offer"}
