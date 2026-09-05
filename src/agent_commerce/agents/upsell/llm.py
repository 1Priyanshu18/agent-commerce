"""Condition C: an LLM decides whether to offer an upsell, given the cart and the merchant's
rules, and must justify the decision either way (offer or no-offer). Forced tool call, same
discipline as the buyer agent's output contract — but no marker fallback here, since Phase 5
doesn't call for one: a malformed or missing decision fails closed to NoOffer.

The AgenticPay paper found buyer-side and seller-side performance are asymmetric — the same
model negotiates differently depending on which role it plays. This strategy is the
seller-side half of that asymmetry; the eval harness (Phase 8) is where it gets measured, but
every decision's full reasoning is preserved here specifically so that comparison is possible
later.
"""

from __future__ import annotations

import logging

from agent_commerce.cart.models import Cart
from agent_commerce.catalog.models import Product
from agent_commerce.catalog.store import CatalogStore
from agent_commerce.core.llm import LLMClient, LLMResponse, Message, ToolChoice, ToolSpec
from agent_commerce.core.money import Money

from .dark_patterns import check_dark_patterns
from .strategy import MerchantRules, NoOffer, Offer, find_candidate_products

logger = logging.getLogger(__name__)

_DECIDE_TOOL = ToolSpec(
    name="upsell_decision",
    description="Decide whether to offer an upsell to the buyer's cart, or make no offer.",
    input_schema={
        "type": "object",
        "properties": {
            "offered": {"type": "boolean"},
            "sku": {"type": ["string", "null"], "description": "Required (non-null) when offered is true."},
            "discount_pct": {
                "type": ["number", "null"],
                "description": "Required (non-null) when offered is true.",
            },
            "reasoning": {
                "type": "string",
                "description": "Justification for this decision — mandatory whether offering or not.",
            },
        },
        "required": ["offered", "sku", "discount_pct", "reasoning"],
        "additionalProperties": False,
    },
)

_SYSTEM_PROMPT_TEMPLATE = (
    "You are the merchant's upsell agent. Given the buyer's current cart and a list of "
    "eligible candidate products, decide whether to offer one complementary product at a "
    "discount, or make no offer. You must justify your decision either way.\n\n"
    "Hard constraints:\n"
    "- Any discount you propose must not exceed {max_discount_pct}% (the merchant's cap).\n"
    "- The resulting margin on the offered item must not fall below {min_margin_pct}% (the "
    "merchant's floor) — margin_pct = (discounted_price - cost) / discounted_price * 100.\n"
    "- Only offer a product from the candidate list — it is already filtered to in-stock, "
    "non-blacklisted, complementary items.\n\n"
    "No dark patterns: do not use false scarcity ('only 1 left', 'almost gone'), countdown "
    "pressure ('hurry', 'act now', 'limited time'), or guilt framing ('you'll regret it', "
    "'don't they deserve...'). Keep the reasoning honest and factual."
)


def _candidate_summary(product: Product) -> dict:
    margin_pct = (product.price_paise - product.cost_paise) / product.price_paise * 100
    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "price": Money(product.price_paise).format_inr(),
        "margin_pct": round(margin_pct, 1),
        "stock": product.stock,
    }


def _build_prompt(cart: Cart, candidates: list[Product]) -> str:
    cart_summary = {
        "items": [{"sku": i.sku, "name": i.name, "quantity": i.quantity} for i in cart.items.values()],
        "subtotal": Money(cart.subtotal_paise).format_inr(),
        "total": Money(cart.total_paise).format_inr(),
        "projected_margin_pct": cart.projected_margin_pct,
    }
    lines = [
        "Current cart:",
        str(cart_summary),
        "",
        "Candidate products (already filtered to in-stock, non-blacklisted, complementary):",
        str([_candidate_summary(p) for p in candidates]) if candidates else "(none available)",
    ]
    return "\n".join(lines)


class LLMStrategy:
    def __init__(self, llm: LLMClient, catalog: CatalogStore) -> None:
        self._llm = llm
        self._catalog = catalog

    def decide(self, cart: Cart, rules: MerchantRules) -> Offer | NoOffer:
        candidates = find_candidate_products(cart, self._catalog, rules)
        if not cart.items or not candidates:
            return NoOffer(reasoning="no complementary in-stock candidate available for this cart")

        system = _SYSTEM_PROMPT_TEMPLATE.format(
            max_discount_pct=rules.max_discount_pct, min_margin_pct=rules.min_margin_pct
        )
        try:
            response = self._llm.complete(
                system=system,
                messages=[Message(role="user", content=_build_prompt(cart, candidates))],
                tools=[_DECIDE_TOOL],
                tool_choice=ToolChoice(mode="specific", tool_name="upsell_decision"),
                max_tokens=1024,
            )
        except Exception as e:  # noqa: BLE001 — this class promises to fail closed to NoOffer
            # on any malformed decision; a request that fails before a response even exists
            # (observed live: Groq's server-side tool-call validation rejecting a response
            # that omits a nullable field entirely instead of sending it as null) is the same
            # kind of failure as a response that parses to nonsense, just caught earlier.
            logger.warning("upsell LLM strategy call failed: %s", e)
            return NoOffer(reasoning=f"parse failure: upsell decision call failed ({e})")

        decision = self._parse(response, candidates, rules)
        check = check_dark_patterns(decision.reasoning)
        if check.flagged:
            logger.warning(
                "upsell LLM strategy reasoning flagged for dark patterns: "
                "categories=%s phrases=%s reasoning=%r",
                check.matched_categories,
                check.matched_phrases,
                decision.reasoning,
            )
        return decision

    def _parse(
        self, response: LLMResponse, candidates: list[Product], rules: MerchantRules
    ) -> Offer | NoOffer:
        tool_call = response.tool_call_by_name("upsell_decision")
        if tool_call is None:
            return NoOffer(reasoning="parse failure: model did not return a decision tool call")

        data = tool_call.arguments or {}
        offered = data.get("offered")
        reasoning = data.get("reasoning") or ""

        if offered is not True:
            return NoOffer(reasoning=reasoning or "model declined to make an offer")

        sku = data.get("sku")
        discount_pct = data.get("discount_pct")
        if not sku or discount_pct is None:
            return NoOffer(reasoning="parse failure: offered=true but sku or discount_pct missing")

        candidate_skus = {p.sku for p in candidates}
        if sku not in candidate_skus:
            return NoOffer(reasoning=f"parse failure: model proposed {sku!r}, not in the candidate list")

        clamped_discount_pct = min(float(discount_pct), rules.max_discount_pct)
        return Offer(sku=sku, discount_pct=round(clamped_discount_pct, 2), reasoning=reasoning)
