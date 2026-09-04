from agent_commerce.orchestrator.negotiation import (
    ForcedClose,
    NegotiationState,
    is_small_gap,
    resolve_small_gap,
    small_gap_threshold_paise,
)


def test_small_gap_threshold_floor_applies_for_small_carts() -> None:
    # 3% of a Rs 500 cart is Rs 15, below the Rs 50 floor.
    assert small_gap_threshold_paise(50000) == 5000


def test_small_gap_threshold_scales_for_large_carts() -> None:
    # 3% of a Rs 10,000 cart is Rs 300, above the Rs 50 floor.
    assert small_gap_threshold_paise(1000000) == 30000


def test_is_small_gap_true_at_boundary() -> None:
    # cart total 50000 -> threshold 5000; gap of exactly 5000 is inclusive.
    assert is_small_gap(offer_price_paise=100000, counter_price_paise=95000, cart_total_paise=50000) is True


def test_is_small_gap_false_just_over_boundary() -> None:
    assert is_small_gap(offer_price_paise=100000, counter_price_paise=94999, cart_total_paise=50000) is False


def test_resolve_small_gap_returns_none_when_gap_is_large() -> None:
    result = resolve_small_gap(
        offer_price_paise=100000,
        counter_price_paise=50000,
        cart_total_before_upsell_paise=50000,
        hard_ceiling_paise=1000000,
    )
    assert result is None


def test_resolve_small_gap_accepts_when_within_ceiling() -> None:
    result = resolve_small_gap(
        offer_price_paise=100000,
        counter_price_paise=98000,
        cart_total_before_upsell_paise=50000,
        hard_ceiling_paise=200000,  # 50000 + 100000 = 150000 <= 200000
    )
    assert result == ForcedClose(decision="ACCEPT")


def test_resolve_small_gap_declines_when_over_ceiling() -> None:
    result = resolve_small_gap(
        offer_price_paise=100000,
        counter_price_paise=98000,
        cart_total_before_upsell_paise=50000,
        hard_ceiling_paise=120000,  # 50000 + 100000 = 150000 > 120000
    )
    assert result == ForcedClose(decision="DECLINE")


def test_resolve_small_gap_boundary_exactly_at_ceiling_accepts() -> None:
    result = resolve_small_gap(
        offer_price_paise=100000,
        counter_price_paise=98000,
        cart_total_before_upsell_paise=50000,
        hard_ceiling_paise=150000,  # exactly equal
    )
    assert result == ForcedClose(decision="ACCEPT")


def test_negotiation_state_backs_off_after_two_declines() -> None:
    state = NegotiationState()
    assert state.can_offer() is True

    state.record_decline()
    assert state.can_offer() is True
    assert state.backed_off is False

    state.record_decline()
    assert state.can_offer() is False
    assert state.backed_off is True


def test_negotiation_state_stays_backed_off_after_further_declines() -> None:
    state = NegotiationState()
    for _ in range(4):
        state.record_decline()
    assert state.decline_count == 4
    assert state.backed_off is True
    assert state.can_offer() is False
