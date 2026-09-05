"""Provider-agnostic types. No module outside core/llm/ may import a vendor SDK or reference
a vendor-specific response shape — everything crosses this boundary as these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Opaque, provider-specific round-trip data (e.g. Gemini's thought_signature, which its API
    # requires to be echoed back verbatim on the next turn for multi-turn tool use). Only the
    # adapter that set it ever reads it back; every other adapter and all orchestrator code
    # ignores it.
    provider_metadata: dict | None = None


@dataclass(frozen=True)
class ToolChoice:
    mode: Literal["auto", "required", "specific"]
    tool_name: str | None = None  # required when mode == "specific"

    def __post_init__(self) -> None:
        if self.mode == "specific" and not self.tool_name:
            raise ValueError("ToolChoice(mode='specific') requires tool_name")


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_call_id: str | None = None  # for role == "tool": which call this responds to
    tool_name: str | None = None  # for role == "tool": the tool that was called


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # normalized: "end_turn" | "tool_use" | "max_tokens" | other provider value
    usage: dict  # {"input_tokens": int, "output_tokens": int}
    provider: str
    model: str
    cached: bool = False

    def tool_call_by_name(self, name: str) -> ToolCall | None:
        for tc in self.tool_calls:
            if tc.name == name:
                return tc
        return None


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResponse: ...
