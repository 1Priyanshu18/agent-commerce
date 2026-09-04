"""A small, reusable harness for running the same cart through each upsell strategy and
comparing margin outcomes — the "session harness" the exit criteria for this phase asks for.
Deliberately not the Phase 8 eval harness (goals.yaml, seeds, statistics) — just enough to
demonstrate the three strategies are interchangeable and produce comparable outcomes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from agent_commerce.cart.models import Cart, CartItem
from agent_commerce.catalog.store import CatalogStore

from .dark_patterns import check_dark_patterns
from .strategy import MerchantRules, NoOffer, Offer, UpsellStrategy


@dataclass(frozen=True)
class StrategyOutcome:
    strategy_name: str
    decision: Offer | NoOffer
    baseline_margin_pct: float
    margin_pct_if_accepted: float | None  # None when the decision is NoOffer
    dark_pattern_flagged: bool


def _margin_if_accepted(cart: Cart, offer: Offer, catalog: CatalogStore) -> float | None:
    product = catalog.get(offer.sku)
    if product is None:
        return None
    simulated = copy.deepcopy(cart)
    discounted_price_paise = round(product.price_paise * (1 - offer.discount_pct / 100))
    simulated.add(
        CartItem(
            sku=product.sku,
            name=product.name,
            unit_price_paise=discounted_price_paise,
            unit_cost_paise=product.cost_paise,
            quantity=1,
        )
    )
    return simulated.projected_margin_pct


def run_comparison(
    strategies: dict[str, UpsellStrategy],
    cart: Cart,
    rules: MerchantRules,
    catalog: CatalogStore,
) -> list[StrategyOutcome]:
    baseline_margin_pct = cart.projected_margin_pct
    outcomes: list[StrategyOutcome] = []
    for name, strategy in strategies.items():
        decision = strategy.decide(cart, rules)
        margin_if_accepted = (
            _margin_if_accepted(cart, decision, catalog) if isinstance(decision, Offer) else None
        )
        outcomes.append(
            StrategyOutcome(
                strategy_name=name,
                decision=decision,
                baseline_margin_pct=baseline_margin_pct,
                margin_pct_if_accepted=margin_if_accepted,
                dark_pattern_flagged=check_dark_patterns(decision.reasoning).flagged,
            )
        )
    return outcomes
