"""Deterministic, scriptable fake — no network calls. Used by the test suite so it never
depends on any real provider being reachable or funded.
"""

from __future__ import annotations

from .types import LLMResponse, Message, ToolCall, ToolChoice, ToolSpec


class FakeLLMClient:
    provider = "fake"

    def __init__(self, responses: list[LLMResponse], *, model: str = "fake-model") -> None:
        self._responses = list(responses)
        self.model = model
        self.calls: list[dict] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no more scripted responses")
        return self._responses.pop(0)


def text_response(text: str, *, model: str = "fake-model") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input_tokens": 0, "output_tokens": 0},
        provider="fake",
        model=model,
    )


def tool_response(
    name: str, arguments: dict, *, call_id: str | None = None, model: str = "fake-model"
) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[ToolCall(id=call_id or f"call_{name}", name=name, arguments=arguments)],
        stop_reason="tool_use",
        usage={"input_tokens": 0, "output_tokens": 0},
        provider="fake",
        model=model,
    )
