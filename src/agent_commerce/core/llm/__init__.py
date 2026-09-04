"""Provider-agnostic LLM layer. Nothing outside core/llm/ imports a vendor SDK or references
a vendor-specific response shape — everything crosses this boundary as the types in .types.

Provider selection is a config value (LLM_PROVIDER), never hardcoded at a call site. Every
LLMResponse carries provider and model, so callers can record which one produced it.
"""

from __future__ import annotations

from pathlib import Path

from agent_commerce.core.config import Config

from .cache import CachingLLMClient
from .fake import FakeLLMClient, text_response, tool_response
from .resilience import CallBudgetExceededError, FatalError, GuardedLLMClient, RetryableError
from .types import LLMClient, LLMResponse, Message, ToolCall, ToolChoice, ToolSpec

__all__ = [
    "LLMClient",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolChoice",
    "ToolSpec",
    "CachingLLMClient",
    "GuardedLLMClient",
    "RetryableError",
    "FatalError",
    "CallBudgetExceededError",
    "FakeLLMClient",
    "text_response",
    "tool_response",
    "build_client",
]

_DEFAULT_CACHE_DIR = Path(".cache/llm")

# Conservative RPM defaults per provider, kept below the published free-tier ceiling to leave
# headroom. Not user-configurable via env — these are safety defaults, not a knob meant to be
# tuned day to day.
_PROVIDER_RPM_DEFAULTS = {"gemini": 8, "groq": 25, "anthropic": 45}


def build_client(
    config: Config, *, cache_dir: Path | str = _DEFAULT_CACHE_DIR, bypass_cache: bool = False
) -> CachingLLMClient:
    provider = config.llm_provider

    if provider == "gemini":
        from .gemini import GeminiLLMClient

        raw: object = GeminiLLMClient(api_key=config.gemini_api_key, model=config.gemini_model)
    elif provider == "groq":
        from .groq import GroqLLMClient

        raw = GroqLLMClient(api_key=config.groq_api_key, model=config.groq_model)
    elif provider == "anthropic":
        from .anthropic import AnthropicLLMClient

        raw = AnthropicLLMClient(api_key=config.anthropic_api_key, model=config.anthropic_model)
    else:
        raise ValueError(
            f"unknown LLM_PROVIDER: {provider!r} (expected 'gemini', 'groq', or 'anthropic'; "
            "'fake' is for tests only — construct FakeLLMClient directly)"
        )

    guarded = GuardedLLMClient(
        raw,
        requests_per_minute=_PROVIDER_RPM_DEFAULTS[provider],
        max_calls_per_run=config.llm_max_calls_per_run,
    )
    return CachingLLMClient(guarded, cache_dir=cache_dir, bypass=bypass_cache)
