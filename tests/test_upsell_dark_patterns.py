from agent_commerce.agents.upsell.dark_patterns import check_dark_patterns


def test_clean_reasoning_is_not_flagged() -> None:
    text = "This is the highest-margin complementary item in stock, discounted within policy."
    result = check_dark_patterns(text)
    assert result.flagged is False
    assert result.matched_categories == ()
    assert result.matched_phrases == ()


def test_false_scarcity_is_flagged() -> None:
    result = check_dark_patterns("Only 1 left in stock, act before it's gone!")
    assert result.flagged is True
    assert "false_scarcity" in result.matched_categories


def test_countdown_pressure_is_flagged() -> None:
    result = check_dark_patterns("Hurry, this offer expires soon!")
    assert result.flagged is True
    assert "countdown_pressure" in result.matched_categories


def test_guilt_framing_is_flagged() -> None:
    result = check_dark_patterns("Don't you want your loved one to have the best? They deserve it.")
    assert result.flagged is True
    assert "guilt_framing" in result.matched_categories


def test_case_insensitive_matching() -> None:
    result = check_dark_patterns("HURRY, ACT NOW before it's gone!")
    assert result.flagged is True


def test_multiple_categories_all_reported() -> None:
    result = check_dark_patterns("Almost gone! Hurry, you'll regret missing this.")
    assert set(result.matched_categories) == {"false_scarcity", "countdown_pressure", "guilt_framing"}


def test_empty_text_is_not_flagged() -> None:
    result = check_dark_patterns("")
    assert result.flagged is False
