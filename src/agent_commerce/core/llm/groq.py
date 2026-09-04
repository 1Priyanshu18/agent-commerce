"""Groq adapter. Secondary free provider — OpenAI-compatible wire format."""

from __future__ import annotations

import json
from typing import Any

import groq

from .resilience import FatalError, RetryableError
from .types import LLMResponse, Message, ToolCall, ToolChoice, ToolSpec


def _messages_to_api(system: str, messages: list[Message]) -> list[dict]:
    api_messages: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            api_messages.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ]
            api_messages.append(entry)
        elif m.role == "tool":
            api_messages.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""})
    return api_messages


def _tool_choice_to_api(tool_choice: ToolChoice | None) -> str | dict | None:
    if tool_choice is None:
        return None
    if tool_choice.mode == "auto":
        return "auto"
    if tool_choice.mode == "required":
        return "required"
    return {"type": "function", "function": {"name": tool_choice.tool_name}}


_FINISH_REASON_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


class GroqLLMClient:
    provider = "groq"

    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        self.model = model
        self._client = client or groq.Groq(api_key=api_key)

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
            "messages": _messages_to_api(system, messages),
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ]
        api_tool_choice = _tool_choice_to_api(tool_choice)
        if api_tool_choice is not None:
            kwargs["tool_choice"] = api_tool_choice

        try:
            response = self._client.chat.completions.create(**kwargs)
        except (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError) as e:
            raise RetryableError(str(e)) from e
        except groq.APIStatusError as e:
            if e.status_code >= 500:
                raise RetryableError(str(e)) from e
            raise FatalError(str(e)) from e

        choice = response.choices[0]
        text = choice.message.content or ""
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
            for tc in (choice.message.tool_calls or [])
        ]
        stop_reason = _FINISH_REASON_MAP.get(choice.finish_reason, choice.finish_reason)
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        }
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            provider="groq",
            model=response.model,
        )
