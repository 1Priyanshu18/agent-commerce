"""Loads eval/results.json for the Streamlit app's Eval tab (Phase 9, docs/PHASE_9_SPEC.md).
Never computes the grid — reads only what eval/runner.py already checkpointed. Pure logic
(no Streamlit import) so it's testable and cacheable via st.cache_data in app.py.
"""

from __future__ import annotations

import json
from pathlib import Path

CONDITIONS = ["none", "rules", "llm"]
ENFORCEMENT_LEVELS = ["tool_level_only", "argument_level"]


def load_eval_results(path: Path | str) -> dict:
    """Returns {"meta": {...}, "sessions": [...]}. Missing file -> empty, valid shape, so
    callers never need a special "not found" branch — an empty eval tab renders the same
    code path as a partial one.
    """
    p = Path(path)
    if not p.exists():
        return {"meta": {}, "sessions": []}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {"meta": data.get("meta", {}), "sessions": data.get("sessions", [])}


def load_injection_results(path: Path | str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"meta": {}, "results": []}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {"meta": data.get("meta", {}), "results": data.get("results", [])}


def cell_coverage(sessions: list[dict]) -> dict[tuple[str, str], int]:
    """How many sessions exist per (condition, enforcement_level) cell — the basis for
    showing which cells completed and which didn't, honestly, per the spec.
    """
    coverage: dict[tuple[str, str], int] = {
        (c, e): 0 for c in CONDITIONS for e in ENFORCEMENT_LEVELS
    }
    for s in sessions:
        key = (s.get("condition"), s.get("enforcement_level"))
        if key in coverage:
            coverage[key] += 1
    return coverage


def goals_covered(sessions: list[dict]) -> dict[str, int]:
    """goal_id -> how many of the 6 cells it has (out of 3 conditions x 2 enforcement
    levels) — lets the UI show "G01: 6/6 complete", "G08: 2/6 partial", etc.
    """
    counts: dict[str, int] = {}
    for s in sessions:
        goal_id = s.get("goal_id")
        if goal_id:
            counts[goal_id] = counts.get(goal_id, 0) + 1
    return counts
