"""Gemini adapter (Google AI Studio, google-genai SDK). Development default — see .env.example."""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as gtypes

from .resilience import FatalError, RetryableError
from .types import LLMResponse, Message, ToolCall, ToolChoice, ToolSpec


def _contents_from_messages(messages: list[Message]) -> list[gtypes.Content]:
    contents: list[gtypes.Content] = []
    in_tool_result_batch = False
    for m in messages:
        if m.role == "user":
            contents.append(gtypes.Content(role="user", parts=[gtypes.Part(text=m.content or "")]))
            in_tool_result_batch = False
        elif m.role == "assistant":
            parts: list[gtypes.Part] = []
            if m.content:
                parts.append(gtypes.Part(text=m.content))
            for tc in m.tool_calls:
                fc = gtypes.FunctionCall(id=tc.id, name=tc.name, args=tc.arguments)
                # Gemini's API requires thought_signature to be echoed back verbatim on
                # multi-turn tool use with thinking-capable models — dropping it is a 400.
                thought_signature = (tc.provider_metadata or {}).get("thought_signature")
                parts.append(gtypes.Part(function_call=fc, thought_signature=thought_signature))
            contents.append(gtypes.Content(role="model", parts=parts))
            in_tool_result_batch = False
        elif m.role == "tool":
            part = gtypes.Part(
                function_response=gtypes.FunctionResponse(
                    id=m.tool_call_id, name=m.tool_name or "", response={"result": m.content or ""}
                )
            )
            # Multiple tool calls in one turn return their results in a single content
            # block, mirroring the model's single function-call-batch turn.
            if in_tool_result_batch:
                contents[-1].parts.append(part)
            else:
                contents.append(gtypes.Content(role="user", parts=[part]))
                in_tool_result_batch = True
    return contents


def _tool_config(tool_choice: ToolChoice | None) -> gtypes.ToolConfig | None:
    if tool_choice is None:
        return None
    if tool_choice.mode == "auto":
        mode, names = gtypes.FunctionCallingConfigMode.AUTO, None
    elif tool_choice.mode == "required":
        mode, names = gtypes.FunctionCallingConfigMode.ANY, None
    else:
        mode, names = gtypes.FunctionCallingConfigMode.ANY, [tool_choice.tool_name]
    fcc = gtypes.FunctionCallingConfig(mode=mode, allowed_function_names=names)
    return gtypes.ToolConfig(function_calling_config=fcc)


_FINISH_REASON_MAP = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}


class GeminiLLMClient:
    provider = "gemini"

    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        self.model = model
        self._client = client or genai.Client(api_key=api_key)

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
        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if tools:
            config_kwargs["tools"] = [
                gtypes.Tool(
                    function_declarations=[
                        gtypes.FunctionDeclaration(
                            name=t.name, description=t.description, parameters_json_schema=t.input_schema
                        )
                        for t in tools
                    ]
                )
            ]
        tool_config = _tool_config(tool_choice)
        if tool_config is not None:
            config_kwargs["tool_config"] = tool_config

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=_contents_from_messages(messages),
                config=gtypes.GenerateContentConfig(**config_kwargs),
            )
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                raise RetryableError(str(e)) from e
            raise FatalError(str(e)) from e
        except genai_errors.ServerError as e:
            raise RetryableError(str(e)) from e

        text = response.text or ""
        # Iterate parts directly (not the response.function_calls convenience property) —
        # thought_signature lives on the same Part as function_call, and the convenience
        # property doesn't carry it.
        response_parts = (
            response.candidates[0].content.parts
            if response.candidates and response.candidates[0].content
            else []
        ) or []
        tool_calls = [
            ToolCall(
                id=part.function_call.id or part.function_call.name or "",
                name=part.function_call.name or "",
                arguments=dict(part.function_call.args or {}),
                provider_metadata=(
                    {"thought_signature": part.thought_signature} if part.thought_signature else None
                ),
            )
            for part in response_parts
            if part.function_call is not None
        ]
        finish_reason = response.candidates[0].finish_reason if response.candidates else None
        finish_name = finish_reason.name if finish_reason is not None else ""
        # Gemini's finish_reason is "STOP" even when the turn ended in a function call (unlike
        # Anthropic/Groq, which have a distinct tool-use finish reason) — tool_calls presence
        # must be checked before falling back to the finish-reason table.
        if tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = _FINISH_REASON_MAP.get(finish_name, finish_name or "end_turn")
        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
            "output_tokens": response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
        }
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            provider="gemini",
            model=self.model,
        )
