from datetime import UTC, datetime

from agent_commerce.core.clock import FixedClock
from agent_commerce.demo.budget import DailyBudgetTracker

DAY1 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
DAY1_LATER = datetime(2026, 9, 5, 22, 0, tzinfo=UTC)
DAY2 = datetime(2026, 9, 6, 1, 0, tzinfo=UTC)


def test_starts_with_full_budget(tmp_path) -> None:
    tracker = DailyBudgetTracker(tmp_path / "budget.json", daily_budget=10, clock=FixedClock(DAY1))
    assert tracker.calls_used_today() == 0
    assert tracker.remaining() == 10
    assert tracker.is_tripped() is False


def test_record_calls_decrements_remaining(tmp_path) -> None:
    tracker = DailyBudgetTracker(tmp_path / "budget.json", daily_budget=10, clock=FixedClock(DAY1))
    tracker.record_calls(3)
    assert tracker.calls_used_today() == 3
    assert tracker.remaining() == 7
    assert tracker.is_tripped() is False


def test_trips_once_budget_reached(tmp_path) -> None:
    tracker = DailyBudgetTracker(tmp_path / "budget.json", daily_budget=5, clock=FixedClock(DAY1))
    tracker.record_calls(5)
    assert tracker.is_tripped() is True
    assert tracker.remaining() == 0


def test_trips_when_calls_exceed_budget_not_just_equal(tmp_path) -> None:
    tracker = DailyBudgetTracker(tmp_path / "budget.json", daily_budget=5, clock=FixedClock(DAY1))
    tracker.record_calls(7)
    assert tracker.is_tripped() is True
    assert tracker.remaining() == 0  # never negative


def test_persists_across_tracker_instances_same_day(tmp_path) -> None:
    path = tmp_path / "budget.json"
    DailyBudgetTracker(path, daily_budget=10, clock=FixedClock(DAY1)).record_calls(4)
    later = DailyBudgetTracker(path, daily_budget=10, clock=FixedClock(DAY1_LATER))
    assert later.calls_used_today() == 4


def test_resets_on_a_new_day(tmp_path) -> None:
    path = tmp_path / "budget.json"
    DailyBudgetTracker(path, daily_budget=10, clock=FixedClock(DAY1)).record_calls(10)
    next_day = DailyBudgetTracker(path, daily_budget=10, clock=FixedClock(DAY2))
    assert next_day.is_tripped() is False
    assert next_day.calls_used_today() == 0


def test_missing_file_is_treated_as_zero_used(tmp_path) -> None:
    tracker = DailyBudgetTracker(tmp_path / "does_not_exist.json", daily_budget=10, clock=FixedClock(DAY1))
    assert tracker.calls_used_today() == 0


def test_corrupt_file_fails_safe_to_zero_used(tmp_path) -> None:
    path = tmp_path / "budget.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    tracker = DailyBudgetTracker(path, daily_budget=10, clock=FixedClock(DAY1))
    assert tracker.calls_used_today() == 0
