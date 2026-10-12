import main
from starlette.requests import Request


def _request(path="/"):
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [],
        "client": ("test", 1234), "server": ("test", 80),
    })


def _mock_lifecycle(monkeypatch):
    monkeypatch.setattr(main.token_service, "refresh_tokens", lambda: None)
    monkeypatch.setattr(
        main,
        "check_upstox_token_validity",
        lambda send_success_message=False: {"status": "success", "valid": True},
    )
    monkeypatch.setattr(main.telegram_token_bot, "start", lambda: True)
    monkeypatch.setattr(main.telegram_token_bot, "stop", lambda: None)
    monkeypatch.setattr(main.telegram_service, "send_shutdown_message", lambda details="": True)


def test_home_and_chart_pages_render(monkeypatch):
    _mock_lifecycle(monkeypatch)
    monkeypatch.setattr(
        "api.chart_routes._chart_payload",
        lambda key: {"candles": [{"time": "2026-10-12T09:15:00+05:30", "open": 1, "high": 2, "low": 1, "close": 2}], "total_candles": 1},
    )
    home = main.home_router.routes[0].endpoint(_request("/"))
    chart_index = main.chart_router.routes[0].endpoint(_request("/charts"))
    chart = main.chart_router.routes[1].endpoint(_request("/chart/NSE_INDEX|Nifty 50"), "NSE_INDEX|Nifty 50")
    assert home.status_code == 200
    assert b"Market Data Foundation" in home.body
    assert chart_index.status_code == 200
    assert chart.status_code == 200
    assert b"NSE_INDEX|Nifty 50" in chart.body
    assert b"/ws/chart" in chart.body


def test_strategy_and_order_routes_are_not_registered():
    paths = {getattr(route, "path", "") for route in main.app.routes}
    forbidden = ("ema", "opening-range", "isolat", "order", "trade", "refresh", "option-chain")
    assert not [path for path in paths if any(term in path.lower() for term in forbidden)]
    assert "/health" in paths
    assert "/token/status" in paths
    assert "/logs" in paths
    assert "/api/logs" in paths
    assert "/ws/chart" in paths
    health = main.health_router.routes[0].endpoint()
    assert health["status"] == "ok"


def test_token_status_api_does_not_return_a_token(monkeypatch):
    monkeypatch.setattr(
        "api.token_store_routes.get_token_status_snapshot",
        lambda: {"status": "success", "monitor_interval_minutes": 30, "token_document": {
            "token_available": True,
            "access_token_masked": "masked-value",
            "last_validation_status": "success",
            "last_profile_user_name": "private-profile-name",
        }},
    )
    from api.token_store_routes import token_status
    response = token_status()
    assert "access_token" not in str(response)
    assert "masked-value" not in str(response)
    assert "private-profile-name" not in str(response)


def test_token_lifecycle_scheduler_is_registered():
    calls = {}

    class Scheduler:
        running = False
        def __init__(self, **kwargs): calls["timezone"] = kwargs["timezone"]
        def add_job(self, func, trigger, **kwargs): calls["job"] = (func, trigger, kwargs)
        def start(self): self.running = True; calls["started"] = True
        def shutdown(self, wait=False): self.running = False; calls["shutdown"] = True

    scheduler = main.build_token_monitor_scheduler(Scheduler)
    assert scheduler.running
    assert calls["job"][0] is main.check_upstox_token_validity
    assert calls["job"][1] == "interval"
    assert calls["job"][2]["id"] == "token_lifecycle_check"
    assert calls["job"][2]["kwargs"]["send_success_message"] is False
    scheduler.shutdown(wait=False)
    assert calls.get("shutdown")


def test_telegram_save_token_command_keeps_validation_and_storage(monkeypatch):
    from token_tasks.telegram_token_bot import TelegramTokenBot

    bot = TelegramTokenBot()
    bot.enabled = True
    bot.chat_id = "42"
    bot.restrict_to_chat = True
    sent = []
    saved = []
    deleted = []
    monkeypatch.setattr(bot, "_send", lambda text, chat_id=None: sent.append(text) or True)
    monkeypatch.setattr(bot, "delete_message", lambda chat_id, message_id: deleted.append((chat_id, message_id)) or True)
    monkeypatch.setattr("token_tasks.telegram_token_bot.upstox_token_client.validate_token", lambda value: (True, {"data": {"user_id": "user"}}, None))
    monkeypatch.setattr("token_tasks.telegram_token_bot.token_store.save_access_token", lambda access_token, source: saved.append((access_token, source)))
    monkeypatch.setattr("token_tasks.telegram_token_bot.token_store.update_validation_status", lambda **kwargs: True)

    bot.handle_message({"chat": {"id": 42}, "message_id": 1, "text": "/save_token"})
    token = "x" * 60
    bot.handle_message({"chat": {"id": 42}, "message_id": 2, "text": token})

    assert saved == [(token, "telegram")]
    assert deleted == [(42, 2)]
    assert all(token not in message for message in sent)
    assert any("saved and validated" in message for message in sent)


def test_telegram_token_status_and_polling_setup(monkeypatch):
    from token_tasks.telegram_token_bot import TelegramTokenBot

    bot = TelegramTokenBot()
    bot.enabled = True
    bot.chat_id = "42"
    bot.restrict_to_chat = True
    sent = []
    requests = []
    monkeypatch.setattr(bot, "_send", lambda text, chat_id=None: sent.append(text) or True)
    monkeypatch.setattr("token_tasks.telegram_token_bot.token_store.get_masked_token_info", lambda: {"token_available": True, "last_validation_status": "success"})
    monkeypatch.setattr(bot, "_request", lambda method, payload=None, timeout=None: requests.append((method, payload, timeout)) or [])

    bot.handle_message({"chat": {"id": 42}, "message_id": 3, "text": "/token_status"})
    bot._offset = 12
    bot.get_updates()
    bot.set_commands()

    assert any("token_available" in message for message in sent)
    assert requests[0][0] == "getUpdates"
    assert requests[0][1]["offset"] == 12
    assert requests[1][0] == "setMyCommands"
    assert {item["command"] for item in requests[1][1]["commands"]} >= {"save_token", "token_status", "token_check"}


def test_telegram_token_check_handler_hides_provider_details(monkeypatch):
    from token_tasks import telegram_token_bot as bot_module
    from token_tasks.telegram_token_bot import TelegramTokenBot

    bot = TelegramTokenBot()
    bot.chat_id = "42"
    bot.restrict_to_chat = True
    sent = []
    monkeypatch.setattr(bot, "_send", lambda text, chat_id=None: sent.append(text) or True)
    monkeypatch.setattr(
        bot_module,
        "check_upstox_token_validity",
        lambda send_success_message=False: {
            "status": "failed",
            "valid": False,
            "message": "Token validation failed.",
            "profile": {"access_token": "must-not-be-sent"},
        },
    )
    bot.handle_message({"chat": {"id": 42}, "message_id": 4, "text": "/token_check"})
    assert sent
    assert "must-not-be-sent" not in sent[0]


def test_log_diagnostics_are_read_only_and_path_limited(monkeypatch):
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from fastapi import HTTPException
    from api import logs_api

    with TemporaryDirectory(dir=Path(__file__).parent) as temp_dir:
        tmp_path = Path(temp_dir)
        monkeypatch.setattr(logs_api, "PROJECT_ROOT", tmp_path)
        folder = tmp_path / "logs"
        folder.mkdir()
        (folder / "app.log").write_text("first\nsecond\n", encoding="utf-8")
        files = logs_api.list_log_files("logs")
        content = logs_api.read_log_file("app.log", "logs", 1)
        assert [item["name"] for item in files["files"]] == ["app.log"]
        assert content["lines"] == ["second"]
        try:
            logs_api.read_log_file("..\\outside.log", "logs", 10)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("A path traversal filename was accepted")
