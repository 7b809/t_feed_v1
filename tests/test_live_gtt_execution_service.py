from services.live_gtt_execution_service import (
    _adjust_stop, _adjust_trailing_stop, _direction, _target_points, _tick,
    _trailing_step, _trailing_stop_for_price,
)


def test_target_bands():
    assert _target_points(220) == 60
    assert _target_points(116) == 50
    assert _target_points(85) == 40


def test_nearest_support_below_calculated_stop_is_selected():
    stop, meta = _adjust_stop(116, 81.2, {"S1": 90, "S2": 80, "S3": 70})
    assert stop == 80
    assert meta["selected_level"] == "S2"
    assert meta["support_adjustment_applied"] is True


def test_support_above_calculated_stop_does_not_widen_stop_loss():
    stop, meta = _adjust_stop(116, 70, {"S1": 90, "S2": 80, "S3": 75})
    assert stop == 70
    assert meta["selected_level"] == "PERCENTAGE"
    assert meta["support_adjustment_applied"] is False


def test_highest_support_below_calculated_stop_wins():
    stop, meta = _adjust_stop(116, 85, {"S1": 90, "S2": 80, "S3": 75})
    assert stop == 80
    assert meta["selected_level"] == "S2"


def test_tick_rounding():
    assert _tick(116.023, 0.05) == 116.0
    assert _tick(116.027, 0.05) == 116.05


def test_signal_direction():
    assert _direction({"signal": "BULLISH"}) == "BULLISH"
    assert _direction({"direction": "bearish"}) == "BEARISH"
    assert _direction({"signal": "unknown"}) is None



def test_gtt2_stop_uses_nearest_support_strictly_below_percentage_stop():
    stop, meta = _adjust_trailing_stop(100, 70, {"S1": 85, "S2": 65, "S3": 55})
    assert stop == 65
    assert meta["selected_level"] == "S2"
    assert meta["nearest_support_name"] == "S2"
    assert meta["nearest_support_value"] == 65
    assert meta["final_stop_loss"] == 65
    assert meta["support_adjustment_applied"] is True

    # All supports are above the percentage stop, so none may widen the stop.
    stop, meta = _adjust_trailing_stop(100, 70, {"S1": 85, "S2": 75, "S3": 72})
    assert stop == 70
    assert meta["selected_level"] == "PERCENTAGE"
    assert meta["nearest_support_name"] is None
    assert meta["nearest_support_value"] is None
    assert meta["support_adjustment_applied"] is False

    # A support exactly at the percentage stop is not a strictly lower support.
    stop, meta = _adjust_trailing_stop(100, 70, {"S1": 70, "S2": 60, "S3": 50})
    assert stop == 60
    assert meta["selected_level"] == "S2"


def test_gtt2_trailing_step_uses_initial_entry_stop_and_target():
    assert _trailing_step(100, 70, 140) == 50


def test_gtt2_trailing_moves_stop_one_fixed_step_per_full_price_step():
    assert _trailing_stop_for_price(100, 70, 50, 149, 0.05) == (70, 0)
    assert _trailing_stop_for_price(100, 70, 50, 150, 0.05) == (120, 1)
    assert _trailing_stop_for_price(100, 70, 50, 200, 0.05) == (170, 2)
    assert _trailing_stop_for_price(100, 70, 50, 249, 0.05) == (170, 2)
    assert _trailing_stop_for_price(100, 70, 50, 250, 0.05) == (220, 3)
