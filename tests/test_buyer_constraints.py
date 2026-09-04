import pytest

from agent_commerce.agents.buyer.constraints import ConstraintExtractionError, extract_constraints
from agent_commerce.core.llm import FakeLLMClient, text_response, tool_response


def _llm(tool_input: dict | None, *, as_text: bool = False) -> FakeLLMClient:
    if as_text:
        response = text_response("I'd rather just chat about this.")
    else:
        response = tool_response("extract_buyer_constraints", tool_input or {})
    return FakeLLMClient([response])


def test_extract_constraints_happy_path() -> None:
    llm = _llm(
        {
            "budget_ceiling_paise": 200000,
            "soft_target_paise": None,
            "category": "Toys & Games",
            "recipient_context": "10-year-old nephew",
            "must_have": ["birthday"],
            "deadline": None,
        }
    )
    constraints = extract_constraints(llm, "Buy a birthday gift under Rs 2000 for my 10-year-old nephew")
    assert constraints.budget_ceiling_paise == 200000
    assert constraints.soft_target_paise is None
    assert constraints.category == "Toys & Games"
    assert constraints.recipient_context == "10-year-old nephew"
    assert constraints.must_have == ["birthday"]
    assert constraints.deadline is None


def test_extract_constraints_with_soft_target() -> None:
    llm = _llm(
        {
            "budget_ceiling_paise": 300000,
            "soft_target_paise": 200000,
            "category": None,
            "recipient_context": None,
            "must_have": [],
            "deadline": "next Friday",
        }
    )
    goal = "Ideally around Rs 2000, but I can stretch to Rs 3000 by next Friday"
    constraints = extract_constraints(llm, goal)
    assert constraints.budget_ceiling_paise == 300000
    assert constraints.soft_target_paise == 200000
    assert constraints.deadline == "next Friday"


def test_extract_constraints_raises_when_model_emits_text_instead_of_tool_call() -> None:
    llm = _llm(None, as_text=True)
    with pytest.raises(ConstraintExtractionError, match="did not return a tool call"):
        extract_constraints(llm, "some goal")


def test_extract_constraints_raises_on_missing_required_field() -> None:
    llm = _llm(
        {
            "soft_target_paise": None,
            "category": None,
            "recipient_context": None,
            "must_have": [],
            "deadline": None,
        }
    )
    with pytest.raises(ConstraintExtractionError, match="malformed"):
        extract_constraints(llm, "some goal")
