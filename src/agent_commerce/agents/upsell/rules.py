"""Baseline B: deterministic rules strategy.

Picks the highest-margin in-stock complement to the cart, then sets
discount = min(needed, cap), where:
  - "needed" is the deepest discount this specific item's margin can sustain while staying at
    or above the merchant's min_margin_pct floor (derived from cost/price alone — this
    requires no buyer-private information, which keeps it a legitimate merchant-side
    computation under the role-separation model).
  - "cap" is the merchant's own max_discount_pct policy ceiling.
"""

from __future__ import annotations

from agent_commerce.cart.models import Cart
from agent_commerce.catalog.models import Product
from agent_commerce.catalog.store import CatalogStore

from .strategy import MerchantRules, NoOffer, Offer, find_candidate_products


def _margin_pct(product: Product) -> float:
    return (product.price_paise - product.cost_paise) / product.price_paise * 100


def _sustainable_discount_pct(product: Product, min_margin_pct: float) -> float:
    """The deepest discount this product can take while its post-discount margin stays at or
    above min_margin_pct. 0 if the product can't be discounted at all without breaching it.
    """
    min_selling_price = product.cost_paise / (1 - min_margin_pct / 100)
    if min_selling_price >= product.price_paise:
        return 0.0
    return (1 - min_selling_price / product.price_paise) * 100


class RulesStrategy:
    def __init__(self, catalog: CatalogStore) -> None:
        self._catalog = catalog

    def decide(self, cart: Cart, rules: MerchantRules) -> Offer | NoOffer:
        if not cart.items:
            return NoOffer(reasoning="cart is empty; nothing to complement")

        candidates = find_candidate_products(cart, self._catalog, rules)
        if not candidates:
            return NoOffer(reasoning="no complementary in-stock item found for this cart")

        best = max(candidates, key=_margin_pct)
        margin_pct = _margin_pct(best)

        needed_discount_pct = _sustainable_discount_pct(best, rules.min_margin_pct)
        discount_pct = round(min(needed_discount_pct, rules.max_discount_pct), 2)

        if discount_pct <= 0:
            return NoOffer(
                reasoning=(
                    f"{best.name} ({best.sku}) is the best complement by margin, but it cannot "
                    f"be discounted at all without breaching the {rules.min_margin_pct}% merchant "
                    "margin floor"
                )
            )

        reasoning = (
            f"{best.name} ({best.sku}) is the highest-margin in-stock complement "
            f"({margin_pct:.1f}% margin) for items already in the cart. Offering a "
            f"{discount_pct:.1f}% discount — the deepest this item supports while staying at "
            f"or above the {rules.min_margin_pct}% merchant margin floor, capped by the "
            f"{rules.max_discount_pct}% merchant discount policy."
        )
        return Offer(sku=best.sku, discount_pct=discount_pct, reasoning=reasoning)
