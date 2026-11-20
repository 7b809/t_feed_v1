from types import SimpleNamespace

import pytest
import requests

from app.services import gtt_order_client


def _config():
    return SimpleNamespace(
        gtt_orders_enabled=True,
        gtt_cleanup_url="https://sandbox.novag7.in/api/external/v1/gtt/cleanup",
        gtt_api_token="x" * 32,
        gtt_request_timeout_seconds=5,
    )


def test_cleanup_without_prior_gtt_is_a_noop(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("No request should be sent for an empty GTT list")

    monkeypatch.setattr(gtt_order_client.requests, "post", fail)
    result = gtt_order_client.cleanup_gtt_orders(_config(), [], isolation_id="iso-1")
    assert result["cleanup_complete"] is True
    assert result["skipped"] == "NO_PRIOR_GTT"


def test_cleanup_posts_only_supplied_gtt_ids(monkeypatch):
    captured = {}

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "cleanup_complete": True, "results": [
                {"gtt_order_id": "GTT-1", "status": "CANCELLED_UNTRIGGERED"}
            ]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gtt_order_client.requests, "post", fake_post)
    result = gtt_order_client.cleanup_gtt_orders(
        _config(), ["GTT-1", "GTT-1"], isolation_id="iso-1", source_order_id="DUMMY-1"
    )
    assert result["cleanup_complete"] is True
    assert captured["url"] == _config().gtt_cleanup_url
    assert captured["json"]["gtt_order_ids"] == ["GTT-1"]
    assert captured["json"]["isolation_id"] == "iso-1"
    assert captured["json"]["api_token"] == "x" * 32


def test_cleanup_transport_timeout_is_unknown(monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(gtt_order_client.requests, "post", timeout)
    with pytest.raises(gtt_order_client.GTTOrderSubmissionError) as exc:
        gtt_order_client.cleanup_gtt_orders(_config(), ["GTT-1"], isolation_id="iso-1")
    assert exc.value.outcome_unknown is True


def test_cleanup_requires_explicit_confirmation(monkeypatch):
    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"success": True, "cleanup_complete": False}

    monkeypatch.setattr(gtt_order_client.requests, "post", lambda *a, **k: Response())
    with pytest.raises(gtt_order_client.GTTOrderSubmissionError):
        gtt_order_client.cleanup_gtt_orders(_config(), ["GTT-1"], isolation_id="iso-1")
