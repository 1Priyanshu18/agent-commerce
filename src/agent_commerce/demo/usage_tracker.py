"""Wraps any LLMClient to record per-call usage (tokens, cache hit/miss). Shared by
eval/runner.py and the Streamlit app's Live run tab — both need to attribute real API usage
to a specific run without the underlying adapter supporting this itself.
"""

from __future__ import annotations


class UsageTrackingLLMClient:
    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self.calls: list[dict] = []

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    @property
    def model(self) -> str:
        return self._wrapped.model

    def complete(self, **kwargs):
        response = self._wrapped.complete(**kwargs)
        self.calls.append({"usage": response.usage, "cached": response.cached})
        return response

    def real_call_count(self) -> int:
        return sum(1 for c in self.calls if not c["cached"])
