"""The buyer's decision output contract on an upsell offer: ACCEPT / DECLINE / COUNTER.

Primary: a forced tool call against RESPOND_TOOL's JSON schema. Fallback: the AgenticPay
structured marker syntax, for when the model emits prose instead of a tool call:

    ### BUYER DECISION(ACCEPT|DECLINE|COUNTER) ###
    ### BUYER PRICE(45000) ###          # paise, only when COUNTER
    ### BUYER REASON(...) ###

Tool call is tried first, marker second. If both fail, the caller (orchestrator) must fail
closed to DECLINE and log a parse_failure ledger entry — never guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from agent_commerce.core.llm import LLMResponse, ToolSpec


class DecisionType(StrEnum):
    ACCEPT = "ACCEPT"
    DECLINE = "DECLINE"
    COUNTER = "COUNTER"


@dataclass(frozen=True)
class BuyerDecision:
    decision: DecisionType
    counter_price_paise: int | None
    reason: str


RESPOND_TOOL = ToolSpec(
    name="respond_to_offer",
    description="Record the buyer's decision on an upsell offer.",
    input_schema={
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["ACCEPT", "DECLINE", "COUNTER"]},
            "counter_price_paise": {
                "type": ["integer", "null"],
                "description": "Required (non-null) only when decision is COUNTER.",
            },
            "reason": {"type": "string"},
        },
        "required": ["decision", "counter_price_paise", "reason"],
        "additionalProperties": False,
    },
)

_MARKER_DECISION_RE = re.compile(r"###\s*BUYER DECISION\((ACCEPT|DECLINE|COUNTER)\)\s*###")
_MARKER_PRICE_RE = re.compile(r"###\s*BUYER PRICE\((\d+)\)\s*###")
_MARKER_REASON_RE = re.compile(r"###\s*BUYER REASON\(([^)]*)\)\s*###")


def _decision_from_dict(data: dict) -> BuyerDecision | None:
    try:
        decision = DecisionType(data["decision"])
    except (KeyError, ValueError):
        return None
    counter_price = data.get("counter_price_paise")
    if decision == DecisionType.COUNTER and counter_price is None:
        return None
    reason = data.get("reason") or ""
    return BuyerDecision(decision=decision, counter_price_paise=counter_price, reason=reason)


def parse_tool_decision(arguments: dict) -> BuyerDecision | None:
    return _decision_from_dict(arguments)


def parse_marker_decision(text: str) -> BuyerDecision | None:
    decision_match = _MARKER_DECISION_RE.search(text)
    if not decision_match:
        return None
    decision = DecisionType(decision_match.group(1))
    price_match = _MARKER_PRICE_RE.search(text)
    counter_price = int(price_match.group(1)) if price_match else None
    if decision == DecisionType.COUNTER and counter_price is None:
        return None
    reason_match = _MARKER_REASON_RE.search(text)
    reason = reason_match.group(1).strip() if reason_match else ""
    return BuyerDecision(decision=decision, counter_price_paise=counter_price, reason=reason)


def parse_buyer_decision(response: LLMResponse) -> tuple[BuyerDecision | None, str]:
    """Tool call first, marker second. Returns (decision_or_none, method), where method is
    one of 'tool_call' / 'marker' / 'none' — the 'none' case is what feeds the parse_failure
    ledger entry and the TUE argument-validity statistic.
    """
    tool_call = response.tool_call_by_name("respond_to_offer")
    if tool_call is not None:
        decision = parse_tool_decision(tool_call.arguments)
        if decision is not None:
            return decision, "tool_call"

    if response.text:
        decision = parse_marker_decision(response.text)
        if decision is not None:
            return decision, "marker"

    return None, "none"
