"""Persistent daily call-budget tracker for the Streamlit demo app's Live run tab (Phase 9,
docs/PHASE_9_SPEC.md) — the deployed Space is public and this exists to cap what it can spend
of the owner's own LLM quota per day, across restarts (Streamlit's own session_state resets
per browser session, so the counter has to live on disk, not in memory).

Pure logic, no Streamlit import.
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
        # A new day resets the counter — this is a rolling calendar-day budget, not a
        # cumulative one.
        return state if state.date == fresh.date else fresh

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
