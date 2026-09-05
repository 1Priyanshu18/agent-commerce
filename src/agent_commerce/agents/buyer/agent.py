"""The buyer agent's LLM-calling surface. It only ever proposes — constraint extraction,
tool-loop turns, and upsell-offer decisions — and never executes a tool or touches policy/
or payments/ itself; the orchestrator does that (see orchestrator/run_session.py).
"""

from __future__ import annotations

from agent_commerce.core.llm import LLMClient, LLMResponse, Message, ToolChoice, ToolSpec
from agent_commerce.core.money import Money

from .constraints import BuyerConstraints, extract_constraints
from .output import RESPOND_TOOL, BuyerDecision, parse_buyer_decision


class BuyerAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def extract_constraints(self, goal_text: str) -> BuyerConstraints:
        return extract_constraints(self._llm, goal_text)

    def system_prompt(self, constraints: BuyerConstraints, *, categories: list[str] | None = None) -> str:
        must_have = ", ".join(constraints.must_have) if constraints.must_have else "none specified"
        soft_target = (
            Money(constraints.soft_target_paise).format_inr() if constraints.soft_target_paise else "none"
        )
        category_hint = (
            f"Valid catalog categories (use one of these exact strings for catalog.search's "
            f"category filter — anything else returns zero results): {', '.join(categories)}\n"
            if categories
            else ""
        )
        return (
            "You are a buyer agent shopping on behalf of a user. Use the available tools to "
            "search the catalog, add exactly one suitable item to the cart, and then call "
            "checkout.confirm.\n\n"
            f"Hard budget ceiling: {Money(constraints.budget_ceiling_paise).format_inr()} — the "
            "cart total must never exceed this.\n"
            f"Soft target (a preference, not a hard limit): {soft_target}\n"
            f"Category: {constraints.category or 'unspecified'}\n"
            f"{category_hint}"
            f"Recipient: {constraints.recipient_context or 'unspecified'}\n"
            f"Must-haves: {must_have}\n"
            f"Deadline: {constraints.deadline or 'none'}\n\n"
            "Search first, review the results, add one suitable item, then confirm checkout. "
            "One or two searches are usually enough — narrow with category/price/tags rather "
            "than issuing many near-duplicate searches, since each turn is limited. If a search "
            "returns zero results, don't keep guessing category strings — search by text/tags "
            "instead, or omit the category filter.\n\n"
            "If a tool call returns an error (e.g. stock_conflict, policy_denied), read its "
            "human_reason and adapt — remove or replace the affected item, or otherwise adjust "
            "the cart — rather than repeating the same call unchanged. In particular, if "
            "checkout.confirm is denied for exceeding the budget ceiling, use cart.remove to "
            "take the current item(s) back out before adding a cheaper replacement — adding "
            "another item on top only makes the total worse. Once cart.add succeeds, call "
            "checkout.confirm immediately — do not keep searching for a better option once "
            "you already have a suitable item in the cart."
        )

    def next_turn(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return self._llm.complete(system=system, messages=messages, tools=tools, max_tokens=2048)

    def decide_on_offer(
        self,
        *,
        constraints: BuyerConstraints,
        cart_total_paise: int,
        offer_sku: str,
        offer_name: str,
        discounted_price_paise: int,
        reasoning: str,
    ) -> tuple[BuyerDecision | None, str]:
        prompt = (
            f"The merchant is offering to add {offer_name} ({offer_sku}) to your cart at a "
            f"discounted price of {Money(discounted_price_paise).format_inr()}. Their reasoning: "
            f"{reasoning!r}. Your current cart total is {Money(cart_total_paise).format_inr()}; your "
            f"hard budget ceiling is {Money(constraints.budget_ceiling_paise).format_inr()}. Decide "
            "whether to ACCEPT, DECLINE, or COUNTER with a different price."
        )
        response = self._llm.complete(
            system=self.system_prompt(constraints),
            messages=[Message(role="user", content=prompt)],
            tools=[RESPOND_TOOL],
            tool_choice=ToolChoice(mode="specific", tool_name="respond_to_offer"),
            max_tokens=1024,
        )
        return parse_buyer_decision(response)
