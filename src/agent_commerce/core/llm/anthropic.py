"""Anthropic adapter. Kept for a final paid run once the harness works on free tiers."""

from __future__ import annotations

from typing import Any

import anthropic

from .resilience import FatalError, RetryableError
from .types import LLMResponse, Message, ToolCall, ToolChoice, ToolSpec


def _messages_to_api(messages: list[Message]) -> list[dict]:
    api_messages: list[dict] = []
    in_tool_result_batch = False
    for m in messages:
        if m.role == "user":
            api_messages.append({"role": "user", "content": m.content or ""})
            in_tool_result_batch = False
        elif m.role == "assistant":
            content: list[dict] = []
            if m.content:
                content.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
            api_messages.append({"role": "assistant", "content": content})
            in_tool_result_batch = False
        elif m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or ""}
            # Multiple tool calls in one turn must return their results in a single user
            # message — splitting them across messages trains the model away from parallel
            # tool use.
            if in_tool_result_batch:
                api_messages[-1]["content"].append(block)
            else:
                api_messages.append({"role": "user", "content": [block]})
                in_tool_result_batch = True
    return api_messages


def _tool_choice_to_api(tool_choice: ToolChoice | None) -> dict | None:
    if tool_choice is None:
        return None
    if tool_choice.mode == "auto":
        return {"type": "auto"}
    if tool_choice.mode == "required":
        return {"type": "any"}
    return {"type": "tool", "name": tool_choice.tool_name}


class AnthropicLLMClient:
    provider = "anthropic"

    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        self.model = model
        self._client = client or anthropic.Anthropic(api_key=api_key)

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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": _messages_to_api(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]
        api_tool_choice = _tool_choice_to_api(tool_choice)
        if api_tool_choice is not None:
            kwargs["tool_choice"] = api_tool_choice

        try:
            response = self._client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            raise RetryableError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise RetryableError(str(e)) from e
            raise FatalError(str(e)) from e

        text = "".join(b.text for b in response.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input)
            for b in response.content
            if b.type == "tool_use"
        ]
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=usage,
            provider="anthropic",
            model=response.model,
        )
