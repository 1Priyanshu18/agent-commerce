"""The buyer-role MCP tool surface. Structurally, this server only ever registers the tools
in ROLE_ALLOWED_TOOLS[Actor.BUYER_AGENT] — an LLM connected to this server cannot even see
upsell.make_offer or upsell.no_offer, let alone call them. authorize() is still called at the
top of every handler as a defense-in-depth check (see mcp/authz.py).

policy.* and payment.* are never imported here. checkout.confirm only records the buyer's
intent to check out (a ledger entry) — the orchestrator runs the policy check and payment
call around this signal.
"""

from __future__ import annotations

from fastmcp import FastMCP

from agent_commerce.cart.service import CartService
from agent_commerce.catalog.service import CatalogService
from agent_commerce.catalog.store import CatalogStore, SearchQuery
from agent_commerce.ledger.models import ActionType, Actor
from agent_commerce.ledger.store import LedgerStore
from agent_commerce.orchestrator.session import SessionRegistry

from .authz import authorize

_ACTOR = Actor.BUYER_AGENT
# Caps what the agent sees per search: an unfiltered query against the full catalog can
# otherwise dump enough tokens into conversation history to overflow a provider's per-request
# limit on a later turn. The ledger's own SEARCH entry still records the true, untruncated
# count (catalog/service.py) — only what's handed back to the LLM is capped.
_MAX_SEARCH_RESULTS = 5


def build_buyer_server(
    *,
    catalog: CatalogStore,
    catalog_service: CatalogService,
    cart_service: CartService,
    sessions: SessionRegistry,
    ledger: LedgerStore,
) -> FastMCP:
    mcp = FastMCP("Agent Commerce — Buyer Tools")

    def _caused_by(transaction_id: str) -> list[str]:
        last = ledger.last_entry_id(transaction_id)
        return [last] if last else []

    def _authz(tool_name: str, transaction_id: str) -> None:
        authorize(
            _ACTOR, tool_name, ledger, transaction_id=transaction_id, caused_by=_caused_by(transaction_id)
        )

    @mcp.tool(name="catalog.search")
    def catalog_search(
        transaction_id: str,
        text: str | None = None,
        category: str | None = None,
        max_price_paise: int | None = None,
        tags: list[str] | None = None,
        age_range: str | None = None,
    ) -> dict:
        _authz("catalog.search", transaction_id)
        query = SearchQuery(
            text=text,
            category=category,
            max_price_paise=max_price_paise,
            tags=tags or [],
            age_range=age_range,
        )
        results, entry = catalog_service.search(
            query, transaction_id=transaction_id, actor=_ACTOR, caused_by=_caused_by(transaction_id)
        )
        total_matches = len(results)
        shown = results[:_MAX_SEARCH_RESULTS]
        response = {
            "entry_id": entry.entry_id,
            # Minimal fields only — enough to pick a candidate or narrow the search further.
            # Full detail (description, tags, variants, cost) is one catalog.get_details call
            # away for whichever specific item the agent is about to commit to.
            "products": [
                {"sku": p.sku, "name": p.name, "price_paise": p.price_paise, "stock": p.stock}
                for p in shown
            ],
            "total_matches": total_matches,
        }
        if total_matches > len(shown):
            response["hint"] = (
                f"showing {len(shown)} of {total_matches} matches — narrow with category/"
                "max_price_paise/tags/text for a more precise set instead of an unfiltered search"
            )
        return response

    @mcp.tool(name="catalog.get_details")
    def catalog_get_details(transaction_id: str, sku: str) -> dict:
        _authz("catalog.get_details", transaction_id)
        product = catalog.get(sku)
        if product is None:
            raise ValueError(f"unknown SKU: {sku}")
        return product.to_dict()

    @mcp.tool(name="cart.add")
    def cart_add(transaction_id: str, sku: str, quantity: int) -> dict:
        _authz("cart.add", transaction_id)
        cart = sessions.get_or_create(transaction_id)
        cart_service.add_item(cart, sku, quantity, actor=_ACTOR, caused_by=_caused_by(transaction_id))
        return cart.to_view()

    @mcp.tool(name="cart.remove")
    def cart_remove(transaction_id: str, sku: str, quantity: int | None = None) -> dict:
        _authz("cart.remove", transaction_id)
        cart = sessions.get_or_create(transaction_id)
        cart_service.remove_item(cart, sku, quantity, actor=_ACTOR, caused_by=_caused_by(transaction_id))
        return cart.to_view()

    @mcp.tool(name="cart.view")
    def cart_view(transaction_id: str) -> dict:
        _authz("cart.view", transaction_id)
        cart = sessions.get_or_create(transaction_id)
        return cart.to_view()

    @mcp.tool(name="upsell.respond")
    def upsell_respond(
        transaction_id: str,
        offer_entry_id: str,
        decision: str,
        counter_price_paise: int | None = None,
    ) -> dict:
        _authz("upsell.respond", transaction_id)
        if decision not in ("ACCEPT", "DECLINE", "COUNTER"):
            raise ValueError(f"decision must be ACCEPT, DECLINE, or COUNTER, got {decision!r}")
        if decision == "COUNTER" and counter_price_paise is None:
            raise ValueError("counter_price_paise is required when decision is COUNTER")
        entry = ledger.append(
            transaction_id=transaction_id,
            caused_by=[offer_entry_id],
            actor=_ACTOR,
            action_type=ActionType.DECISION,
            input={"decision": decision, "counter_price_paise": counter_price_paise},
            output={"decision": decision},
        )
        return {"entry_id": entry.entry_id, "decision": decision}

    @mcp.tool(name="checkout.confirm")
    def checkout_confirm(transaction_id: str) -> dict:
        _authz("checkout.confirm", transaction_id)
        cart = sessions.get_or_create(transaction_id)
        if not cart.items:
            raise ValueError("cannot check out an empty cart")
        cart_view = cart.to_view()
        ledger.append(
            transaction_id=transaction_id,
            caused_by=_caused_by(transaction_id),
            actor=_ACTOR,
            action_type=ActionType.DECISION,
            input={"stage": "checkout_confirm"},
            output=cart_view,
            resulting_state=cart_view,
        )
        return {"status": "checkout_requested", "cart": cart_view}

    return mcp
