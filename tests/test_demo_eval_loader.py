import json

from agent_commerce.demo.eval_loader import (
    cell_coverage,
    goals_covered,
    load_eval_results,
    load_injection_results,
)


def test_missing_results_file_returns_empty_valid_shape(tmp_path) -> None:
    result = load_eval_results(tmp_path / "does_not_exist.json")
    assert result == {"meta": {}, "sessions": []}


def test_loads_real_shape(tmp_path) -> None:
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps({"meta": {"provider": "groq"}, "sessions": [{"cell_id": "a"}]}), encoding="utf-8"
    )
    result = load_eval_results(path)
    assert result["meta"]["provider"] == "groq"
    assert result["sessions"] == [{"cell_id": "a"}]


def test_missing_injection_results_returns_empty_valid_shape(tmp_path) -> None:
    result = load_injection_results(tmp_path / "does_not_exist.json")
    assert result == {"meta": {}, "results": []}


def test_cell_coverage_counts_per_condition_enforcement() -> None:
    sessions = [
        {"condition": "none", "enforcement_level": "tool_level_only"},
        {"condition": "none", "enforcement_level": "tool_level_only"},
        {"condition": "rules", "enforcement_level": "argument_level"},
    ]
    coverage = cell_coverage(sessions)
    assert coverage[("none", "tool_level_only")] == 2
    assert coverage[("rules", "argument_level")] == 1
    assert coverage[("llm", "argument_level")] == 0


def test_goals_covered_counts_sessions_per_goal() -> None:
    sessions = [
        {"goal_id": "G01"},
        {"goal_id": "G01"},
        {"goal_id": "G05"},
    ]
    assert goals_covered(sessions) == {"G01": 2, "G05": 1}
