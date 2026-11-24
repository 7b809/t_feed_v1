from services.live_gtt_execution_service import _adjust_stop, _direction, _target_points, _tick, _trailing_parameters


def test_target_bands():
    assert _target_points(220) == 60
    assert _target_points(116) == 50
    assert _target_points(85) == 40


def test_support_adjustment():
    stop, meta = _adjust_stop(116, 81.2, {"S1": 90, "S2": 80, "S3": 70})
    assert stop == 80
    assert meta["selected_level"] == "S2"


def test_s3_floor():
    stop, meta = _adjust_stop(116, 70, {"S1": 90, "S2": 80, "S3": 75})
    assert stop == 75
    assert meta["selected_level"] == "S3_FLOOR"


def test_tick_rounding():
    assert _tick(116.023, 0.05) == 116.0
    assert _tick(116.027, 0.05) == 116.05


def test_signal_direction():
    assert _direction({"signal": "BULLISH"}) == "BULLISH"
    assert _direction({"direction": "bearish"}) == "BEARISH"
    assert _direction({"signal": "unknown"}) is None


def test_trailing_parameters_are_derived_from_calculated_stop_and_target():
    assert _trailing_parameters(100, 70, 60, 0.05) == {
        "stop_loss_distance": 30.0,
        "target_distance": 300.0,
        "trailing_jump": 30.0,
    }


def test_trailing_parameters_use_support_adjusted_stop():
    assert _trailing_parameters(100, 82.5, 50, 0.05) == {
        "stop_loss_distance": 17.5,
        "target_distance": 250.0,
        "trailing_jump": 25.0,
    }
