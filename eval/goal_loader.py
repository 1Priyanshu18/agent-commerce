"""Loads eval/goals.yaml into typed Goal objects. Each goal carries ground truth so
outcomes label automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).parent / "goals.yaml"


@dataclass(frozen=True)
class Goal:
    goal_id: str
    category: str
    goal_text: str
    budget_ceiling_paise: int
    compliant_purchase_possible: bool
    satisfying_skus: list[str]
    notes: str = ""


def load_goals(path: Path | str = _DEFAULT_PATH, *, limit: int | None = None) -> list[Goal]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    goals = [
        Goal(
            goal_id=g["goal_id"],
            category=g["category"],
            goal_text=g["goal_text"],
            budget_ceiling_paise=g["budget_ceiling_paise"],
            compliant_purchase_possible=g["compliant_purchase_possible"],
            satisfying_skus=g["satisfying_skus"],
            notes=g.get("notes", ""),
        )
        for g in data["goals"]
    ]
    return goals[:limit] if limit is not None else goals
