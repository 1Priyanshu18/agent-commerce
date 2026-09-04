"""Constraint extraction: turn a buyer's natural-language goal into a typed BuyerConstraints
struct via a forced tool call. The hard/soft split matters twice — the policy engine only
ever enforces budget_ceiling_paise, and the gap to soft_target_paise is exactly where buyer
concession (accepting an upsell that pushes the total above the soft target) gets measured.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_commerce.core.llm import LLMClient, Message, ToolChoice, ToolSpec


@dataclass(frozen=True)
class BuyerConstraints:
    budget_ceiling_paise: int
    soft_target_paise: int | None
    category: str | None
    recipient_context: str | None
    must_have: list[str]
    deadline: str | None


class ConstraintExtractionError(Exception):
    """The LLM's constraint extraction could not be parsed into BuyerConstraints. The caller
    should log a parse_failure ledger entry and end the session — there is no sensible
    fail-closed default for "how much can this buyer spend".
    """


_EXTRACT_TOOL = ToolSpec(
    name="extract_buyer_constraints",
    description="Extract structured purchasing constraints from the buyer's stated goal.",
    input_schema={
        "type": "object",
        "properties": {
            "budget_ceiling_paise": {
                "type": "integer",
                "description": (
                    "The hard maximum the buyer will spend, in paise (1 rupee = 100 paise). "
                    "Never exceed this when interpreting the goal."
                ),
            },
            "soft_target_paise": {
                "type": ["integer", "null"],
                "description": (
                    "A softer, preferred spending target in paise, if the goal implies one "
                    "distinct from the hard ceiling. Exceeding this is not a violation."
                ),
            },
            "category": {"type": ["string", "null"], "description": "Product category, if implied."},
            "recipient_context": {
                "type": ["string", "null"],
                "description": "Who the purchase is for, e.g. '10-year-old nephew'.",
            },
            "must_have": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hard requirements the product must satisfy.",
            },
            "deadline": {"type": ["string", "null"], "description": "Any deadline mentioned, verbatim."},
        },
        "required": [
            "budget_ceiling_paise",
            "soft_target_paise",
            "category",
            "recipient_context",
            "must_have",
            "deadline",
        ],
        "additionalProperties": False,
    },
)

_SYSTEM_PROMPT = (
    "You extract structured purchasing constraints from a buyer's stated goal. "
    "budget_ceiling_paise is the hard maximum the policy engine will enforce — infer it "
    "conservatively from any rupee figure mentioned. If the buyer states a single spending "
    "figure with no further qualification, treat it as the hard ceiling and leave "
    "soft_target_paise null; only set soft_target_paise when the goal genuinely implies a "
    "softer preferred amount distinct from the hard limit."
)


def extract_constraints(llm: LLMClient, goal_text: str) -> BuyerConstraints:
    response = llm.complete(
        system=_SYSTEM_PROMPT,
        messages=[Message(role="user", content=goal_text)],
        tools=[_EXTRACT_TOOL],
        tool_choice=ToolChoice(mode="specific", tool_name="extract_buyer_constraints"),
        max_tokens=1024,
    )
    tool_call = response.tool_call_by_name("extract_buyer_constraints")
    if tool_call is None:
        raise ConstraintExtractionError("model did not return a tool call for constraint extraction")

    data = tool_call.arguments or {}
    try:
        return BuyerConstraints(
            budget_ceiling_paise=int(data["budget_ceiling_paise"]),
            soft_target_paise=(
                int(data["soft_target_paise"]) if data.get("soft_target_paise") is not None else None
            ),
            category=data.get("category"),
            recipient_context=data.get("recipient_context"),
            must_have=list(data.get("must_have") or []),
            deadline=data.get("deadline"),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConstraintExtractionError(f"malformed constraint extraction output: {e}") from e
