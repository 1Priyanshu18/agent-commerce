"""Baseline A: never makes an offer."""

from __future__ import annotations

from agent_commerce.cart.models import Cart

from .strategy import MerchantRules, NoOffer, Offer


class NoneStrategy:
    def decide(self, cart: Cart, rules: MerchantRules) -> Offer | NoOffer:
        return NoOffer(reasoning="baseline: this configuration never makes upsell offers")
