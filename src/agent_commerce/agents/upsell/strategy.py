"""The upsell strategy protocol and shared types. Three interchangeable implementations
(none, rules, llm) all satisfy UpsellStrategy — a session harness can swap between them by
config alone.

Strategies are pure with respect to session state: decide() is a stateless function of
(cart, rules), called at most once per session by the caller. "One offer per session" and
"two declines and the upsell agent backs off" are session-level round-caps enforced by
whatever's driving the session (see orchestrator/negotiation.py), not by the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_commerce.cart.models import Cart
from agent_commerce.catalog.models import Product
from agent_commerce.catalog.store import CatalogStore


@dataclass(frozen=True)
class MerchantRules:
    max_discount_pct: float
    min_margin_pct: float
    blacklist_skus: frozenset[str]


@dataclass(frozen=True)
class Offer:
    sku: str
    discount_pct: float
    reasoning: str


@dataclass(frozen=True)
class NoOffer:
    reasoning: str
    # None means the strategy genuinely decided not to offer and said so. Any other value
    # names a distinct failure mode that fell back to NoOffer rather than a real decision —
    # e.g. "UPSELL_DECISION_CALL_FAILED" when the LLM call itself failed before a response
    # existed to parse. Without this, a silent fallback and a genuine decline are
    # indistinguishable in the ledger (see docs/PROGRESS.md, "a silent-degradation
    # explainability gap").
    machine_reason: str | None = None


class UpsellStrategy(Protocol):
    def decide(self, cart: Cart, rules: MerchantRules) -> Offer | NoOffer: ...


def find_candidate_products(cart: Cart, catalog: CatalogStore, rules: MerchantRules) -> list[Product]:
    """Complementary, in-stock, non-blacklisted products not already in the cart. Shared by
    the rules and llm strategies so both choose from the same pool — the comparison is about
    which product/discount each picks, not about who can browse the catalog better.
    """
    if not cart.items:
        return []

    cart_skus = set(cart.items.keys())
    cart_categories: set[str] = set()
    cart_tags: set[str] = set()
    for sku in cart_skus:
        product = catalog.get(sku)
        if product is not None:
            cart_categories.add(product.category)
            cart_tags.update(product.tags)

    return [
        p
        for p in catalog.all()
        if p.sku not in cart_skus
        and p.sku not in rules.blacklist_skus
        and p.stock > 0
        and (p.category in cart_categories or set(p.tags) & cart_tags)
    ]
