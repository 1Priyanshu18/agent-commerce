"""Normalization tests for each provider adapter: fake raw SDK clients returning
recorded-fixture-shaped payloads (mimicking each SDK's actual response objects), verifying
correct translation into our normalized LLMResponse. No live API calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import httpx2
import pytest

from agent_commerce.core.llm.resilience import FatalError, RetryableError
from agent_commerce.core.llm.types import Message, ToolCall, ToolChoice, ToolSpec

# ============================== Anthropic ==============================


@dataclass
class _AnthropicBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None


@dataclass
class _AnthropicUsage:
    input_tokens: int = 10
    output_tokens: int = 5


@dataclass
class _AnthropicMessage:
    content: list[_AnthropicBlock]
    model: str = "claude-haiku-4-5-20251001"
    stop_reason: str = "end_turn"
    usage: _AnthropicUsage = field(default_factory=_AnthropicUsage)


class _FakeAnthropicMessages:
    def __init__(self, response_or_error) -> None:
        self._response_or_error = response_or_error
        self.received_kwargs: dict | None = None

    def create(self, **kwargs):
        self.received_kwargs = kwargs
        if isinstance(self._response_or_error, Exception):
            raise self._response_or_error
        return self._response_or_error


class _FakeAnthropicSDK:
    def __init__(self, response_or_error) -> None:
        self.messages = _FakeAnthropicMessages(response_or_error)


def test_anthropic_normalizes_text_response() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    raw = _AnthropicMessage(content=[_AnthropicBlock(type="text", text="hello there")])
    client = AnthropicLLMClient(api_key="x", model="claude-haiku-4-5-20251001", client=_FakeAnthropicSDK(raw))

    response = client.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert response.text == "hello there"
    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"
    assert response.provider == "anthropic"
    assert response.usage == {"input_tokens": 10, "output_tokens": 5}


def test_anthropic_normalizes_tool_use_response() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    raw = _AnthropicMessage(
        content=[_AnthropicBlock(type="tool_use", id="toolu_1", name="my_tool", input={"a": 1})],
        stop_reason="tool_use",
    )
    client = AnthropicLLMClient(api_key="x", model="claude-haiku-4-5-20251001", client=_FakeAnthropicSDK(raw))

    response = client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
        tool_choice=ToolChoice(mode="specific", tool_name="my_tool"),
    )

    assert response.tool_calls == [ToolCall(id="toolu_1", name="my_tool", arguments={"a": 1})]
    assert response.stop_reason == "tool_use"


def test_anthropic_sends_forced_tool_choice_in_expected_shape() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    raw = _AnthropicMessage(content=[_AnthropicBlock(type="text", text="x")])
    sdk = _FakeAnthropicSDK(raw)
    client = AnthropicLLMClient(api_key="x", model="m", client=sdk)

    client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
        tool_choice=ToolChoice(mode="specific", tool_name="my_tool"),
    )

    assert sdk.messages.received_kwargs["tool_choice"] == {"type": "tool", "name": "my_tool"}


def test_anthropic_groups_parallel_tool_results_into_one_message() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    raw = _AnthropicMessage(content=[_AnthropicBlock(type="text", text="x")])
    sdk = _FakeAnthropicSDK(raw)
    client = AnthropicLLMClient(api_key="x", model="m", client=sdk)

    messages = [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            tool_calls=(
                ToolCall(id="c1", name="t1", arguments={}),
                ToolCall(id="c2", name="t2", arguments={}),
            ),
        ),
        Message(role="tool", content="result1", tool_call_id="c1", tool_name="t1"),
        Message(role="tool", content="result2", tool_call_id="c2", tool_name="t2"),
    ]
    client.complete(system="sys", messages=messages)

    api_messages = sdk.messages.received_kwargs["messages"]
    tool_result_messages = [m for m in api_messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


def test_anthropic_classifies_rate_limit_as_retryable() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(429, request=req)
    import anthropic

    error = anthropic.RateLimitError("rate limited", response=resp, body=None)
    client = AnthropicLLMClient(api_key="x", model="m", client=_FakeAnthropicSDK(error))

    with pytest.raises(RetryableError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])


def test_anthropic_classifies_bad_request_as_fatal() -> None:
    from agent_commerce.core.llm.anthropic import AnthropicLLMClient

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(400, request=req)
    import anthropic

    error = anthropic.BadRequestError("bad request", response=resp, body=None)
    client = AnthropicLLMClient(api_key="x", model="m", client=_FakeAnthropicSDK(error))

    with pytest.raises(FatalError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])


# ================================ Groq ==================================


@dataclass
class _GroqFunction:
    name: str
    arguments: str


@dataclass
class _GroqToolCall:
    id: str
    function: _GroqFunction


@dataclass
class _GroqMessage:
    content: str | None
    tool_calls: list[_GroqToolCall] | None = None


@dataclass
class _GroqChoice:
    message: _GroqMessage
    finish_reason: str = "stop"


@dataclass
class _GroqUsage:
    prompt_tokens: int = 20
    completion_tokens: int = 8


@dataclass
class _GroqCompletion:
    choices: list[_GroqChoice]
    model: str = "llama-3.3-70b-versatile"
    usage: _GroqUsage = field(default_factory=_GroqUsage)


class _FakeGroqCompletions:
    def __init__(self, response_or_error) -> None:
        self._response_or_error = response_or_error
        self.received_kwargs: dict | None = None

    def create(self, **kwargs):
        self.received_kwargs = kwargs
        if isinstance(self._response_or_error, Exception):
            raise self._response_or_error
        return self._response_or_error


class _FakeGroqChat:
    def __init__(self, response_or_error) -> None:
        self.completions = _FakeGroqCompletions(response_or_error)


class _FakeGroqSDK:
    def __init__(self, response_or_error) -> None:
        self.chat = _FakeGroqChat(response_or_error)


def test_groq_normalizes_text_response() -> None:
    from agent_commerce.core.llm.groq import GroqLLMClient

    raw = _GroqCompletion(choices=[_GroqChoice(message=_GroqMessage(content="hello from groq"))])
    client = GroqLLMClient(api_key="x", model="llama-3.3-70b-versatile", client=_FakeGroqSDK(raw))

    response = client.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert response.text == "hello from groq"
    assert response.stop_reason == "end_turn"
    assert response.provider == "groq"
    assert response.usage == {"input_tokens": 20, "output_tokens": 8}


def test_groq_normalizes_tool_call_response_parsing_json_arguments() -> None:
    from agent_commerce.core.llm.groq import GroqLLMClient

    raw = _GroqCompletion(
        choices=[
            _GroqChoice(
                message=_GroqMessage(
                    content=None,
                    tool_calls=[
                        _GroqToolCall(
                            id="call_1", function=_GroqFunction(name="my_tool", arguments='{"a": 1}')
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    client = GroqLLMClient(api_key="x", model="m", client=_FakeGroqSDK(raw))

    response = client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
    )

    assert response.tool_calls == [ToolCall(id="call_1", name="my_tool", arguments={"a": 1})]
    assert response.stop_reason == "tool_use"


def test_groq_sends_forced_tool_choice_in_openai_shape() -> None:
    from agent_commerce.core.llm.groq import GroqLLMClient

    raw = _GroqCompletion(choices=[_GroqChoice(message=_GroqMessage(content="x"))])
    sdk = _FakeGroqSDK(raw)
    client = GroqLLMClient(api_key="x", model="m", client=sdk)

    client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
        tool_choice=ToolChoice(mode="specific", tool_name="my_tool"),
    )

    expected = {"type": "function", "function": {"name": "my_tool"}}
    assert sdk.chat.completions.received_kwargs["tool_choice"] == expected


def test_groq_classifies_rate_limit_as_retryable() -> None:
    from agent_commerce.core.llm.groq import GroqLLMClient

    req = httpx.Request("POST", "https://api.groq.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    import groq

    error = groq.RateLimitError("rate limited", response=resp, body=None)
    client = GroqLLMClient(api_key="x", model="m", client=_FakeGroqSDK(error))

    with pytest.raises(RetryableError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])


def test_groq_classifies_bad_request_as_fatal() -> None:
    from agent_commerce.core.llm.groq import GroqLLMClient

    req = httpx.Request("POST", "https://api.groq.com/v1/chat/completions")
    resp = httpx.Response(400, request=req)
    import groq

    error = groq.BadRequestError("bad request", response=resp, body=None)
    client = GroqLLMClient(api_key="x", model="m", client=_FakeGroqSDK(error))

    with pytest.raises(FatalError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])


# =============================== Gemini =================================


class _GeminiFinishReason:
    def __init__(self, name: str) -> None:
        self.name = name


@dataclass
class _GeminiFunctionCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class _GeminiPart:
    function_call: _GeminiFunctionCall | None = None
    thought_signature: bytes | None = None
    text: str | None = None


@dataclass
class _GeminiContent:
    parts: list[_GeminiPart] = field(default_factory=list)


@dataclass
class _GeminiCandidate:
    finish_reason: _GeminiFinishReason
    content: _GeminiContent = field(default_factory=_GeminiContent)


@dataclass
class _GeminiUsageMetadata:
    prompt_token_count: int = 15
    candidates_token_count: int = 6


@dataclass
class _GeminiResponse:
    text: str
    candidates: list[_GeminiCandidate]
    usage_metadata: _GeminiUsageMetadata = field(default_factory=_GeminiUsageMetadata)


class _FakeGeminiModels:
    def __init__(self, response_or_error) -> None:
        self._response_or_error = response_or_error
        self.received_kwargs: dict | None = None

    def generate_content(self, **kwargs):
        self.received_kwargs = kwargs
        if isinstance(self._response_or_error, Exception):
            raise self._response_or_error
        return self._response_or_error


class _FakeGeminiSDK:
    def __init__(self, response_or_error) -> None:
        self.models = _FakeGeminiModels(response_or_error)


def test_gemini_normalizes_text_response() -> None:
    from agent_commerce.core.llm.gemini import GeminiLLMClient

    raw = _GeminiResponse(
        text="hello from gemini",
        candidates=[_GeminiCandidate(_GeminiFinishReason("STOP"))],
    )
    client = GeminiLLMClient(api_key="x", model="gemini-2.5-flash", client=_FakeGeminiSDK(raw))

    response = client.complete(system="sys", messages=[Message(role="user", content="hi")])

    assert response.text == "hello from gemini"
    assert response.stop_reason == "end_turn"
    assert response.provider == "gemini"
    assert response.usage == {"input_tokens": 15, "output_tokens": 6}


def test_gemini_normalizes_function_call_response() -> None:
    from agent_commerce.core.llm.gemini import GeminiLLMClient

    part = _GeminiPart(function_call=_GeminiFunctionCall(name="my_tool", args={"a": 1}, id="fc_1"))
    raw = _GeminiResponse(
        text="",
        candidates=[_GeminiCandidate(_GeminiFinishReason("STOP"), content=_GeminiContent(parts=[part]))],
    )
    client = GeminiLLMClient(api_key="x", model="m", client=_FakeGeminiSDK(raw))

    response = client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
    )

    assert response.tool_calls == [ToolCall(id="fc_1", name="my_tool", arguments={"a": 1})]
    assert response.stop_reason == "tool_use"


def test_gemini_carries_thought_signature_into_provider_metadata() -> None:
    # Gemini's API requires this to be echoed back verbatim on the next turn for multi-turn
    # tool use with thinking-capable models — dropping it is a 400 (confirmed live).
    from agent_commerce.core.llm.gemini import GeminiLLMClient

    part = _GeminiPart(
        function_call=_GeminiFunctionCall(name="my_tool", args={"a": 1}, id="fc_1"),
        thought_signature=b"opaque-signature-bytes",
    )
    raw = _GeminiResponse(
        text="",
        candidates=[_GeminiCandidate(_GeminiFinishReason("STOP"), content=_GeminiContent(parts=[part]))],
    )
    client = GeminiLLMClient(api_key="x", model="m", client=_FakeGeminiSDK(raw))

    response = client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
    )

    assert response.tool_calls[0].provider_metadata == {"thought_signature": b"opaque-signature-bytes"}


def test_gemini_echoes_thought_signature_back_on_the_next_turn() -> None:
    from agent_commerce.core.llm.gemini import GeminiLLMClient

    raw = _GeminiResponse(
        text="ok",
        candidates=[_GeminiCandidate(_GeminiFinishReason("STOP"), content=_GeminiContent(parts=[]))],
    )
    sdk = _FakeGeminiSDK(raw)
    client = GeminiLLMClient(api_key="x", model="m", client=sdk)

    messages = [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id="fc_1",
                    name="my_tool",
                    arguments={"a": 1},
                    provider_metadata={"thought_signature": b"opaque-signature-bytes"},
                ),
            ),
        ),
        Message(role="tool", content="result", tool_call_id="fc_1", tool_name="my_tool"),
    ]
    client.complete(system="sys", messages=messages)

    contents = sdk.models.received_kwargs["contents"]
    model_content = next(c for c in contents if c.role == "model")
    fc_part = next(p for p in model_content.parts if p.function_call is not None)
    assert fc_part.thought_signature == b"opaque-signature-bytes"


def test_gemini_sends_forced_tool_choice_via_allowed_function_names() -> None:
    from agent_commerce.core.llm.gemini import GeminiLLMClient

    candidates = [_GeminiCandidate(_GeminiFinishReason("STOP"), content=_GeminiContent(parts=[]))]
    raw = _GeminiResponse(text="x", candidates=candidates)
    sdk = _FakeGeminiSDK(raw)
    client = GeminiLLMClient(api_key="x", model="m", client=sdk)

    client.complete(
        system="sys",
        messages=[Message(role="user", content="hi")],
        tools=[ToolSpec(name="my_tool", description="d", input_schema={})],
        tool_choice=ToolChoice(mode="specific", tool_name="my_tool"),
    )

    tool_config = sdk.models.received_kwargs["config"].tool_config
    assert tool_config.function_calling_config.allowed_function_names == ["my_tool"]


def test_gemini_classifies_429_client_error_as_retryable() -> None:
    from google.genai import errors as genai_errors

    from agent_commerce.core.llm.gemini import GeminiLLMClient

    error = genai_errors.ClientError(code=429, response_json={"error": "rate limited"})
    client = GeminiLLMClient(api_key="x", model="m", client=_FakeGeminiSDK(error))

    with pytest.raises(RetryableError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])


def test_gemini_classifies_400_client_error_as_fatal() -> None:
    from google.genai import errors as genai_errors

    from agent_commerce.core.llm.gemini import GeminiLLMClient

    error = genai_errors.ClientError(code=400, response_json={"error": "bad request"})
    client = GeminiLLMClient(api_key="x", model="m", client=_FakeGeminiSDK(error))

    with pytest.raises(FatalError):
        client.complete(system="sys", messages=[Message(role="user", content="hi")])
