"""The merchant-role MCP tool surface. cart.read_at_checkout is a read-only projection —
this server registers no cart-mutating tool at all, so the upsell side structurally cannot
alter the cart, only observe it and propose an offer via upsell.make_offer / upsell.no_offer.

policy.* and payment.* are never imported here, same as the buyer server.
"""

from __future__ import annotations

from fastmcp import FastMCP

from agent_commerce.catalog.store import CatalogStore
from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.orchestrator.session import SessionRegistry

from .authz import authorize

_ACTOR = Actor.UPSELL_AGENT


def build_merchant_server(
    *,
    catalog: CatalogStore,
    sessions: SessionRegistry,
    ledger: LedgerStore,
) -> FastMCP:
    mcp = FastMCP("Agent Commerce — Merchant Tools")

    def _caused_by(transaction_id: str) -> list[str]:
        last = ledger.last_entry_id(transaction_id)
        return [last] if last else []

    def _authz(tool_name: str, transaction_id: str) -> None:
        authorize(
            _ACTOR, tool_name, ledger, transaction_id=transaction_id, caused_by=_caused_by(transaction_id)
        )

    @mcp.tool(name="cart.read_at_checkout")
    def cart_read_at_checkout(transaction_id: str) -> dict:
        _authz("cart.read_at_checkout", transaction_id)
        cart = sessions.get_or_create(transaction_id)
        return cart.to_view()

    @mcp.tool(name="upsell.make_offer")
    def upsell_make_offer(transaction_id: str, sku: str, discount_pct: float, reasoning: str) -> dict:
        _authz("upsell.make_offer", transaction_id)
        product = catalog.get(sku)
        if product is None:
            raise ValueError(f"unknown SKU: {sku}")
        if product.stock <= 0:
            raise ValueError(f"{sku} is out of stock")
        if not (0 <= discount_pct <= 100):
            raise ValueError("discount_pct must be within [0, 100]")
        if not reasoning.strip():
            raise ValueError("reasoning is required for every upsell offer")

        entry = ledger.append(
            transaction_id=transaction_id,
            caused_by=_caused_by(transaction_id),
            actor=_ACTOR,
            action_type=ActionType.OFFER,
            input={"sku": sku, "discount_pct": discount_pct},
            output={"offered": True, "sku": sku, "discount_pct": discount_pct},
            reasoning_summary=reasoning,
        )
        return {"entry_id": entry.entry_id, "offered": True, "sku": sku, "discount_pct": discount_pct}

    @mcp.tool(name="upsell.no_offer")
    def upsell_no_offer(transaction_id: str, reasoning: str) -> dict:
        _authz("upsell.no_offer", transaction_id)
        if not reasoning.strip():
            raise ValueError("reasoning is required even when declining to make an offer")

        entry = ledger.append(
            transaction_id=transaction_id,
            caused_by=_caused_by(transaction_id),
            actor=_ACTOR,
            action_type=ActionType.OFFER,
            input={},
            output={"offered": False},
            reasoning_summary=reasoning,
        )
        return {"entry_id": entry.entry_id, "offered": False}

    return mcp
