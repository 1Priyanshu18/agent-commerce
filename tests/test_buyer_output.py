import jsonschema
import pytest

from agent_commerce.agents.buyer.output import (
    RESPOND_TOOL,
    DecisionType,
    parse_buyer_decision,
    parse_marker_decision,
    parse_tool_decision,
)
from agent_commerce.core.llm import LLMResponse, ToolCall


def _response(*, text: str = "", tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls or [],
        stop_reason="end_turn",
        usage={},
        provider="fake",
        model="fake",
    )


# --- schema regression: a non-COUNTER response must be a valid tool call ---


def test_respond_tool_schema_accepts_accept_without_counter_price() -> None:
    # Regression test for the same nullable-field bug class as upsell_decision: Groq's
    # server-side validation rejects a call that omits a required key entirely, and a
    # genuine ACCEPT/DECLINE naturally omits counter_price_paise.
    jsonschema.validate(
        instance={"decision": "ACCEPT", "reason": "good deal"}, schema=RESPOND_TOOL.input_schema
    )


def test_respond_tool_schema_accepts_decline_without_counter_price() -> None:
    jsonschema.validate(
        instance={"decision": "DECLINE", "reason": "not interested"}, schema=RESPOND_TOOL.input_schema
    )


def test_respond_tool_schema_accepts_counter_with_price() -> None:
    jsonschema.validate(
        instance={"decision": "COUNTER", "counter_price_paise": 45000, "reason": "too much"},
        schema=RESPOND_TOOL.input_schema,
    )


def test_respond_tool_schema_still_requires_decision_and_reason() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"reason": "x"}, schema=RESPOND_TOOL.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"decision": "ACCEPT"}, schema=RESPOND_TOOL.input_schema)


# --- tool-call parsing ---


def test_parse_tool_decision_accept() -> None:
    decision = parse_tool_decision({"decision": "ACCEPT", "counter_price_paise": None, "reason": "good deal"})
    assert decision.decision == DecisionType.ACCEPT
    assert decision.counter_price_paise is None
    assert decision.reason == "good deal"


def test_parse_tool_decision_counter_requires_price() -> None:
    assert parse_tool_decision({"decision": "COUNTER", "counter_price_paise": None, "reason": "x"}) is None


def test_parse_tool_decision_counter_with_price() -> None:
    data = {"decision": "COUNTER", "counter_price_paise": 45000, "reason": "too much"}
    decision = parse_tool_decision(data)
    assert decision.decision == DecisionType.COUNTER
    assert decision.counter_price_paise == 45000


def test_parse_tool_decision_invalid_enum_value() -> None:
    assert parse_tool_decision({"decision": "MAYBE", "counter_price_paise": None, "reason": ""}) is None


def test_parse_tool_decision_missing_key() -> None:
    assert parse_tool_decision({"counter_price_paise": None, "reason": ""}) is None


# --- marker parsing ---


def test_parse_marker_decision_accept() -> None:
    text = "### BUYER DECISION(ACCEPT) ###\n### BUYER REASON(fits my budget) ###"
    decision = parse_marker_decision(text)
    assert decision.decision == DecisionType.ACCEPT
    assert decision.reason == "fits my budget"


def test_parse_marker_decision_counter_with_price() -> None:
    text = (
        "### BUYER DECISION(COUNTER) ###\n"
        "### BUYER PRICE(45000) ###\n"
        "### BUYER REASON(splitting the difference) ###"
    )
    decision = parse_marker_decision(text)
    assert decision.decision == DecisionType.COUNTER
    assert decision.counter_price_paise == 45000
    assert decision.reason == "splitting the difference"


def test_parse_marker_decision_counter_without_price_fails() -> None:
    text = "### BUYER DECISION(COUNTER) ###\n### BUYER REASON(no price given) ###"
    assert parse_marker_decision(text) is None


def test_parse_marker_decision_missing_marker_returns_none() -> None:
    assert parse_marker_decision("I think I'll pass on this one, thanks.") is None


def test_parse_marker_decision_reason_optional() -> None:
    decision = parse_marker_decision("### BUYER DECISION(DECLINE) ###")
    assert decision.decision == DecisionType.DECLINE
    assert decision.reason == ""


# --- combined tool-call-first, marker-fallback contract ---


def test_parse_buyer_decision_prefers_tool_call() -> None:
    response = _response(
        text="### BUYER DECISION(DECLINE) ###",
        tool_calls=[
            ToolCall(
                id="t1",
                name="respond_to_offer",
                arguments={"decision": "ACCEPT", "counter_price_paise": None, "reason": "tool wins"},
            )
        ],
    )
    decision, method = parse_buyer_decision(response)
    assert method == "tool_call"
    assert decision.decision == DecisionType.ACCEPT
    assert decision.reason == "tool wins"


def test_parse_buyer_decision_falls_back_to_marker_when_no_tool_call() -> None:
    text = "### BUYER DECISION(COUNTER) ### ### BUYER PRICE(30000) ###"
    response = _response(text=text)
    decision, method = parse_buyer_decision(response)
    assert method == "marker"
    assert decision.decision == DecisionType.COUNTER
    assert decision.counter_price_paise == 30000


def test_parse_buyer_decision_falls_back_to_marker_when_tool_call_malformed() -> None:
    response = _response(
        text="### BUYER DECISION(DECLINE) ###",
        tool_calls=[
            ToolCall(
                id="t1",
                name="respond_to_offer",
                arguments={"decision": "COUNTER", "counter_price_paise": None, "reason": "missing price"},
            )
        ],
    )
    decision, method = parse_buyer_decision(response)
    assert method == "marker"
    assert decision.decision == DecisionType.DECLINE


def test_parse_buyer_decision_fails_closed_to_none_when_both_fail() -> None:
    response = _response(text="I'm not sure what to do here.")
    decision, method = parse_buyer_decision(response)
    assert decision is None
    assert method == "none"


def test_parse_buyer_decision_ignores_tool_call_from_a_different_tool() -> None:
    tc = ToolCall(id="t1", name="some_other_tool", arguments={"decision": "ACCEPT"})
    response = _response(tool_calls=[tc])
    decision, method = parse_buyer_decision(response)
    assert decision is None
    assert method == "none"
