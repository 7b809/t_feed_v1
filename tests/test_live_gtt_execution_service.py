from services.live_gtt_execution_service import _adjust_stop, _direction, _target_points, _tick


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
