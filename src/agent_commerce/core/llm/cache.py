"""Disk-backed cache keyed by a hash of the request, so re-running the eval while debugging
the harness costs nothing after the first pass. Wraps any LLMClient — a cache hit never
reaches the wrapped client, so it costs no rate-limit slot, no retry, and no budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_commerce.core.json_canonical import canonical_json

from .types import LLMResponse, Message, ToolCall, ToolChoice, ToolSpec

_DEFAULT_CACHE_DIR = Path(".cache/llm")


def _message_to_dict(m: Message) -> dict:
    return {
        "role": m.role,
        "content": m.content,
        "tool_calls": [asdict(tc) for tc in m.tool_calls],
        "tool_call_id": m.tool_call_id,
        "tool_name": m.tool_name,
    }


def _tool_to_dict(t: ToolSpec) -> dict:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _tool_choice_to_dict(tc: ToolChoice | None) -> dict | None:
    return {"mode": tc.mode, "tool_name": tc.tool_name} if tc is not None else None


class CachingLLMClient:
    def __init__(
        self, wrapped: Any, *, cache_dir: Path | str = _DEFAULT_CACHE_DIR, bypass: bool = False
    ) -> None:
        self._wrapped = wrapped
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._bypass = bypass

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    @property
    def model(self) -> str:
        return self._wrapped.model

    def _cache_path(self, payload: dict) -> Path:
        key = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return self._cache_dir / f"{key}.json"

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
        payload = {
            "provider": self._wrapped.provider,
            "model": self._wrapped.model,
            "system": system,
            "messages": [_message_to_dict(m) for m in messages],
            "tools": [_tool_to_dict(t) for t in (tools or [])],
            "tool_choice": _tool_choice_to_dict(tool_choice),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        cache_path = self._cache_path(payload)

        if not self._bypass and cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return LLMResponse(
                text=cached["text"],
                tool_calls=[ToolCall(**tc) for tc in cached["tool_calls"]],
                stop_reason=cached["stop_reason"],
                usage=cached["usage"],
                provider=cached["provider"],
                model=cached["model"],
                cached=True,
            )

        response = self._wrapped.complete(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        cache_path.write_text(
            json.dumps(
                {
                    "text": response.text,
                    "tool_calls": [asdict(tc) for tc in response.tool_calls],
                    "stop_reason": response.stop_reason,
                    "usage": response.usage,
                    "provider": response.provider,
                    "model": response.model,
                }
            ),
            encoding="utf-8",
        )
        return response
