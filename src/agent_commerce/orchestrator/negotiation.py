"""Negotiation heuristics live here, in code, not in the buyer agent's prompt — the AgenticPay
paper found that even frontier models fail to converge when the price gap is small, so this
is not left to emergent LLM behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

_SMALL_GAP_MIN_PAISE = 5000  # ₹50
_SMALL_GAP_PCT = 0.03
DECLINE_ROUND_CAP = 2


def small_gap_threshold_paise(cart_total_paise: int) -> int:
    return max(_SMALL_GAP_MIN_PAISE, round(cart_total_paise * _SMALL_GAP_PCT))


def is_small_gap(offer_price_paise: int, counter_price_paise: int, cart_total_paise: int) -> bool:
    return abs(offer_price_paise - counter_price_paise) <= small_gap_threshold_paise(cart_total_paise)


@dataclass(frozen=True)
class ForcedClose:
    decision: str  # "ACCEPT" or "DECLINE"
    machine_reason: str = "CLOSED_BY_SMALL_GAP_HEURISTIC"


def resolve_small_gap(
    *,
    offer_price_paise: int,
    counter_price_paise: int,
    cart_total_before_upsell_paise: int,
    hard_ceiling_paise: int,
) -> ForcedClose | None:
    """If the gap between the merchant's offer and the buyer's counter is small enough
    (max(₹50, 3% of the cart total) — measured against the pre-upsell cart total), force an
    immediate close rather than letting negotiation continue: ACCEPT if the resulting cart
    total stays within the buyer's hard budget ceiling, else DECLINE. Returns None when the
    gap isn't small — negotiation should proceed normally.
    """
    if not is_small_gap(offer_price_paise, counter_price_paise, cart_total_before_upsell_paise):
        return None
    projected_total_paise = cart_total_before_upsell_paise + offer_price_paise
    if projected_total_paise <= hard_ceiling_paise:
        return ForcedClose(decision="ACCEPT")
    return ForcedClose(decision="DECLINE")


@dataclass
class NegotiationState:
    """Per-session round tracking for the decline cap: two declines and the upsell agent
    backs off permanently for the rest of the session.
    """

    decline_count: int = 0
    backed_off: bool = False

    def record_decline(self) -> None:
        self.decline_count += 1
        if self.decline_count >= DECLINE_ROUND_CAP:
            self.backed_off = True

    def can_offer(self) -> bool:
        return not self.backed_off
