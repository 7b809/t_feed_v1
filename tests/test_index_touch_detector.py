from services.index_touch_detector import IndexTouchDetector


def test_r3_isolation_uses_index_r3_midpoint_threshold_only():
    detector = IndexTouchDetector()
    levels = {
        "R2": 130.0, "R3": 140.0, "R3_Threshold": 135.0,
        "S2": 90.0, "S3": 80.0, "S3_Threshold": 85.0,
    }
    assert detector.detect("NSE_INDEX|Nifty 50", 134.99, levels) is None
    touch = detector.detect("NSE_INDEX|Nifty 50", 135.0, levels)
    assert touch is not None
    assert touch["side"] == "R3"
    assert touch["trigger_level_name"] == "R3_Threshold"
    assert touch["trigger_level"] == 135.0


def test_s3_isolation_uses_index_s3_midpoint_threshold_only():
    detector = IndexTouchDetector()
    levels = {
        "R2": 130.0, "R3": 140.0, "R3_Threshold": 135.0,
        "S2": 90.0, "S3": 80.0, "S3_Threshold": 85.0,
    }
    assert detector.detect("NSE_INDEX|Nifty 50", 85.01, levels) is None
    touch = detector.detect("NSE_INDEX|Nifty 50", 85.0, levels)
    assert touch is not None
    assert touch["side"] == "S3"
    assert touch["trigger_level_name"] == "S3_Threshold"
    assert touch["trigger_level"] == 85.0


def test_missing_index_threshold_does_not_fall_back_to_raw_r3_s3():
    detector = IndexTouchDetector()
    levels = {"R3": 140.0, "S3": 80.0}
    assert detector.detect("NSE_INDEX|Nifty 50", 150.0, levels) is None
    assert detector.detect("NSE_INDEX|Nifty 50", 70.0, levels) is None


def test_index_or_calculator_thresholds_are_midpoints():
    from core.or_levels_calculator import compute_or_levels

    levels = compute_or_levels({"high": 120.0, "low": 100.0})
    assert levels["R3_Threshold"] == (levels["R2"] + levels["R3"]) / 2
    assert levels["S3_Threshold"] == (levels["S2"] + levels["S3"]) / 2
