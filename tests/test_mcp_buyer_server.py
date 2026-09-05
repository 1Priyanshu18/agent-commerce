from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError, ToolError

from agent_commerce.ledger.models import ActionType, Actor

pytestmark = pytest.mark.anyio


async def test_catalog_search_returns_products_and_logs_search(mcp_stack: SimpleNamespace) -> None:
    result = await mcp_stack.buyer_mcp.call_tool(
        "catalog.search", {"transaction_id": "txn_1", "category": "Toys & Games", "max_price_paise": 200000}
    )
    data = result.structured_content
    assert len(data["products"]) > 0
    assert all(p["category"] == "Toys & Games" for p in data["products"])

    entries = mcp_stack.ledger.entries_for_transaction("txn_1")
    assert len(entries) == 1
    assert entries[0].action_type == ActionType.SEARCH
    assert entries[0].actor == Actor.BUYER_AGENT


async def test_catalog_search_caps_results_returned_to_the_agent(mcp_stack: SimpleNamespace) -> None:
    # An unfiltered (or over-broad) search against the full catalog must not hand the agent
    # every product — that's enough tokens to overflow a provider's per-request limit on the
    # next turn (observed live: 72 products blew a Groq free-tier 8,000 TPM cap outright). The
    # ledger's own SEARCH entry still records the true count; only the tool response is capped.
    result = await mcp_stack.buyer_mcp.call_tool("catalog.search", {"transaction_id": "txn_1"})
    data = result.structured_content

    assert len(data["products"]) <= 10
    assert data["total_matches"] > len(data["products"])
    assert "hint" in data

    entries = mcp_stack.ledger.entries_for_transaction("txn_1")
    assert entries[0].output["count"] == data["total_matches"]


async def test_catalog_get_details_returns_product(mcp_stack: SimpleNamespace) -> None:
    result = await mcp_stack.buyer_mcp.call_tool(
        "catalog.get_details", {"transaction_id": "txn_1", "sku": "SKU-0001"}
    )
    assert result.structured_content["sku"] == "SKU-0001"
    # A pure read: no ledger entry.
    assert mcp_stack.ledger.entries_for_transaction("txn_1") == []


async def test_catalog_get_details_unknown_sku_raises(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="unknown SKU"):
        await mcp_stack.buyer_mcp.call_tool(
            "catalog.get_details", {"transaction_id": "txn_1", "sku": "SKU-9999"}
        )


async def test_cart_add_mutates_cart_and_chains_to_prior_search(mcp_stack: SimpleNamespace) -> None:
    search_result = await mcp_stack.buyer_mcp.call_tool(
        "catalog.search", {"transaction_id": "txn_1", "category": "Toys & Games"}
    )
    search_entry_id = search_result.structured_content["entry_id"]

    add_result = await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 2}
    )
    cart_view = add_result.structured_content
    assert cart_view["items"][0]["sku"] == "SKU-0001"
    assert cart_view["items"][0]["quantity"] == 2

    entries = mcp_stack.ledger.entries_for_transaction("txn_1")
    add_entry = entries[-1]
    assert add_entry.action_type == ActionType.SELECT
    assert add_entry.caused_by == [search_entry_id]


async def test_cart_remove(mcp_stack: SimpleNamespace) -> None:
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 3}
    )
    result = await mcp_stack.buyer_mcp.call_tool(
        "cart.remove", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 1}
    )
    assert result.structured_content["items"][0]["quantity"] == 2


async def test_cart_view_does_not_write_ledger_entry(mcp_stack: SimpleNamespace) -> None:
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 1}
    )
    before = len(mcp_stack.ledger.entries_for_transaction("txn_1"))
    result = await mcp_stack.buyer_mcp.call_tool("cart.view", {"transaction_id": "txn_1"})
    after = len(mcp_stack.ledger.entries_for_transaction("txn_1"))
    assert after == before
    assert result.structured_content["items"][0]["sku"] == "SKU-0001"


async def test_upsell_respond_records_decision(mcp_stack: SimpleNamespace) -> None:
    result = await mcp_stack.buyer_mcp.call_tool(
        "upsell.respond",
        {"transaction_id": "txn_1", "offer_entry_id": "entry_offer_abc", "decision": "ACCEPT"},
    )
    assert result.structured_content["decision"] == "ACCEPT"
    entry = mcp_stack.ledger.entries_for_transaction("txn_1")[-1]
    assert entry.action_type == ActionType.DECISION
    assert entry.caused_by == ["entry_offer_abc"]


async def test_upsell_respond_counter_requires_price(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="counter_price_paise is required"):
        await mcp_stack.buyer_mcp.call_tool(
            "upsell.respond",
            {"transaction_id": "txn_1", "offer_entry_id": "entry_offer_abc", "decision": "COUNTER"},
        )


async def test_upsell_respond_rejects_invalid_decision(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="decision must be"):
        await mcp_stack.buyer_mcp.call_tool(
            "upsell.respond",
            {"transaction_id": "txn_1", "offer_entry_id": "entry_offer_abc", "decision": "MAYBE"},
        )


async def test_checkout_confirm_empty_cart_raises(mcp_stack: SimpleNamespace) -> None:
    with pytest.raises(ToolError, match="empty cart"):
        await mcp_stack.buyer_mcp.call_tool("checkout.confirm", {"transaction_id": "txn_1"})


async def test_checkout_confirm_records_intent(mcp_stack: SimpleNamespace) -> None:
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0001", "quantity": 1}
    )
    result = await mcp_stack.buyer_mcp.call_tool("checkout.confirm", {"transaction_id": "txn_1"})
    assert result.structured_content["status"] == "checkout_requested"
    entry = mcp_stack.ledger.entries_for_transaction("txn_1")[-1]
    assert entry.action_type == ActionType.DECISION
    assert entry.input == {"stage": "checkout_confirm"}


async def test_buyer_server_does_not_expose_merchant_tools(mcp_stack: SimpleNamespace) -> None:
    # Primary defense: structural separation. The tool isn't even registered here.
    with pytest.raises(NotFoundError):
        await mcp_stack.buyer_mcp.call_tool(
            "upsell.make_offer",
            {"transaction_id": "txn_1", "sku": "SKU-0001", "discount_pct": 10, "reasoning": "x"},
        )
    with pytest.raises(NotFoundError):
        await mcp_stack.buyer_mcp.call_tool("cart.read_at_checkout", {"transaction_id": "txn_1"})


async def test_verify_chain_stays_valid_across_a_full_buyer_session(mcp_stack: SimpleNamespace) -> None:
    await mcp_stack.buyer_mcp.call_tool("catalog.search", {"transaction_id": "txn_1", "category": "Books"})
    await mcp_stack.buyer_mcp.call_tool(
        "cart.add", {"transaction_id": "txn_1", "sku": "SKU-0012", "quantity": 1}
    )
    await mcp_stack.buyer_mcp.call_tool("checkout.confirm", {"transaction_id": "txn_1"})
    assert mcp_stack.ledger.verify_chain().ok is True
