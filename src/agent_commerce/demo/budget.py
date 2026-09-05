"""Persistent daily call-budget tracker for the Live run tab, caching the deployed app's own
LLM spend across restarts. Streamlit's session_state resets per browser session, so this
lives on disk instead. Pure logic, no Streamlit import.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_commerce.core.clock import Clock, SystemClock


@dataclass
class BudgetState:
    date: str
    calls_used: int


class DailyBudgetTracker:
    def __init__(self, path: Path | str, *, daily_budget: int, clock: Clock | None = None) -> None:
        self._path = Path(path)
        self._daily_budget = daily_budget
        self._clock = clock or SystemClock()

    def _today(self) -> str:
        return self._clock.now().date().isoformat()

    def _load(self) -> BudgetState:
        fresh = BudgetState(date=self._today(), calls_used=0)
        if not self._path.exists():
            return fresh
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return fresh
        state = BudgetState(date=data.get("date", ""), calls_used=data.get("calls_used", 0))
        return state if state.date == fresh.date else fresh  # a new day resets the counter

    def _save(self, state: BudgetState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"date": state.date, "calls_used": state.calls_used}), encoding="utf-8"
        )

    def calls_used_today(self) -> int:
        return self._load().calls_used

    def daily_budget(self) -> int:
        return self._daily_budget

    def remaining(self) -> int:
        return max(0, self._daily_budget - self.calls_used_today())

    def is_tripped(self) -> bool:
        return self.calls_used_today() >= self._daily_budget

    def record_calls(self, count: int = 1) -> None:
        state = self._load()
        state.calls_used += count
        self._save(state)
