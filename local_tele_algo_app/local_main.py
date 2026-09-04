import asyncio
import html
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    Body, FastAPI, HTTPException, Query, Request, Response, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from local_tele_algo_app.storage import (
    ALGO_EVENTS_FILE, DATA_DIR, MAX_RECORDS_PER_FILE, TELEGRAM_MESSAGES_FILE,
    clear_algo_events, clear_all_records, clear_telegram_messages,
    delete_algo_event, delete_telegram_message, ensure_storage,
    get_algo_event, get_algo_event_count, get_algo_events,
    get_storage_status, get_telegram_message, get_telegram_message_count,
    get_telegram_messages, get_utc_timestamp, make_json_safe,
    save_algo_event, save_telegram_message,
)

APP_TITLE = "Local Telegram and Algo App Simulator"
APP_VERSION = "1.0.0"

HOST = str(os.getenv("LOCAL_ALERT_APP_HOST", "127.0.0.1")).strip()
PORT = max(1, int(os.getenv("LOCAL_ALERT_APP_PORT", "9000")))
DEFAULT_LIST_LIMIT = max(1, min(int(os.getenv("LOCAL_ALERT_DEFAULT_LIST_LIMIT", "100")), MAX_RECORDS_PER_FILE))
MAX_DELAY_SECONDS = max(0.0, float(os.getenv("LOCAL_ALERT_MAX_DELAY_SECONDS", "60")))
REQUIRE_LOCAL_TEST_HEADER = str(os.getenv("LOCAL_ALERT_REQUIRE_TEST_HEADER", "false")).strip().lower() == "true"
EXPECTED_SOURCE = str(os.getenv("LOCAL_ALERT_EXPECTED_SOURCE", "")).strip()

app = FastAPI(
    title=APP_TITLE,
    description="Local receiver for Telegram notifications and Algo App EMA payloads.",
    version=APP_VERSION,
)

# Enable CORS for local test dashboard and API requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TelegramRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    channel: str = "telegram"
    delivery_mode: str = "local_test"
    source: str = "option_feed_engine_local_test"
    title: str = "Untitled Notification"
    level: str = "INFO"
    context: str = "not_available"
    message: str
    parse_mode: str = "HTML"
    disable_web_page_preview: bool = True
    market_time: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    telegram_payload: dict[str, Any] | None = None

class GenericPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def build_message_id() -> str:
    return "local-telegram-" + uuid.uuid4().hex[:16]

def build_event_id() -> str:
    return "local-algo-" + uuid.uuid4().hex[:16]

def normalize_status_code(requested_status_code: int | None, default_status_code: int = 500) -> int:
    if requested_status_code is None:
        return default_status_code
    normalized_status_code = int(requested_status_code)
    if normalized_status_code < 400 or normalized_status_code > 599:
        raise HTTPException(status_code=422, detail="status_code must be between 400 and 599.")
    return normalized_status_code

async def apply_simulated_delay(delay_seconds: float) -> None:
    normalized_delay = max(0.0, float(delay_seconds))
    if normalized_delay > MAX_DELAY_SECONDS:
        raise HTTPException(status_code=422, detail=f"delay_seconds cannot exceed {MAX_DELAY_SECONDS}.")
    if normalized_delay > 0:
        await asyncio.sleep(normalized_delay)

def validate_local_request(request: Request) -> None:
    if REQUIRE_LOCAL_TEST_HEADER:
        local_test_header = str(request.headers.get("X-Local-Test", "")).strip().lower()
        if local_test_header != "true":
            raise HTTPException(status_code=403, detail="X-Local-Test=true header is required.")
    if EXPECTED_SOURCE:
        request_source = str(request.headers.get("X-Local-Test-Source", "") or request.headers.get("X-Source", "")).strip()
        if request_source and request_source != EXPECTED_SOURCE:
            raise HTTPException(status_code=403, detail="Local alert source is not allowed.")

def get_request_metadata(request: Request) -> dict:
    client_host, client_port = None, None
    if request.client is not None:
        client_host = request.client.host
        client_port = request.client.port
    return {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.url.query),
        "client_host": client_host,
        "client_port": client_port,
        "user_agent": request.headers.get("user-agent"),
        "content_type": request.headers.get("content-type"),
        "x_local_test": request.headers.get("x-local-test"),
        "x_delivery_mode": request.headers.get("x-delivery-mode"),
        "x_local_test_source": request.headers.get("x-local-test-source"),
        "x_event_id": request.headers.get("x-event-id"),
        "idempotency_key": request.headers.get("idempotency-key"),
        "x_event_type": request.headers.get("x-event-type"),
        "x_payload_schema_version": request.headers.get("x-payload-schema-version"),
    }

def extract_event_id(payload: dict, request: Request) -> str:
    candidates = (payload.get("event_id"), request.headers.get("X-Event-ID"), request.headers.get("Idempotency-Key"))
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip()
        if normalized_candidate:
            return normalized_candidate
    return build_event_id()

def extract_instrument_key(payload: dict) -> str | None:
    instrument = payload.get("instrument")
    if isinstance(instrument, dict):
        instrument_key = str(instrument.get("instrument_key") or "").strip()
        if instrument_key:
            return instrument_key
    instrument_key = str(payload.get("instrument_key") or "").strip()
    return instrument_key or None

def extract_cross_type(payload: dict) -> str | None:
    ema_payload = payload.get("ema")
    if isinstance(ema_payload, dict):
        cross_type = str(ema_payload.get("cross_type") or "").strip()
        if cross_type:
            return cross_type
    cross_type = str(payload.get("cross_type") or "").strip()
    return cross_type or None

def extract_suggested_side(payload: dict) -> str | None:
    order_suggestion = payload.get("order_suggestion")
    if not isinstance(order_suggestion, dict):
        return None
    candidates = (order_suggestion.get("suggested_order_side"), order_suggestion.get("option_type"), order_suggestion.get("side"))
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip().upper()
        if normalized_candidate:
            return normalized_candidate
    return None

def get_payload_counts(payload: dict) -> tuple[int, int]:
    order_suggestion = payload.get("order_suggestion")
    if not isinstance(order_suggestion, dict):
        return 0, 0
    nearest_instruments = order_suggestion.get("nearest_instruments", [])
    if not isinstance(nearest_instruments, list):
        nearest_instruments = []
    budget_filter = order_suggestion.get("budget_filter", {})
    if not isinstance(budget_filter, dict):
        budget_filter = {}
    budget_instruments = budget_filter.get("instruments", [])
    if not isinstance(budget_instruments, list):
        budget_instruments = []
    return len(nearest_instruments), len(budget_instruments)

def strip_html_tags(value: Any) -> str:
    source_text = str(value or "")
    output = []
    inside_tag = False
    for ch in source_text:
        if ch == '<':
            inside_tag = True
            continue
        if ch == '>':
            inside_tag = False
            continue
        if not inside_tag:
            output.append(ch)
    return html.unescape(''.join(output))

def truncate_text(value: Any, maximum_length: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= maximum_length:
        return text
    return text[:maximum_length].rstrip() + "..."

def get_dashboard_html() -> str:
    telegram_messages = get_telegram_messages(limit=DEFAULT_LIST_LIMIT, newest_first=True)
    algo_events = get_algo_events(limit=DEFAULT_LIST_LIMIT, newest_first=True)
    telegram_rows = []
    for record in telegram_messages:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        telegram_rows.append(f"""
            <tr>
                <td>{html.escape(str(record.get("received_at") or ""))}</td>
                <td>{html.escape(str(record.get("message_id") or ""))}</td>
                <td>{html.escape(str(payload.get("level") or ""))}</td>
                <td>{html.escape(str(payload.get("title") or ""))}</td>
                <td>{html.escape(truncate_text(strip_html_tags(payload.get("message"))))}</td>
            </tr>
            """)
    algo_rows = []
    for record in algo_events:
        algo_rows.append(f"""
            <tr>
                <td>{html.escape(str(record.get("received_at") or ""))}</td>
                <td>{html.escape(str(record.get("event_id") or ""))}</td>
                <td>{html.escape(str(record.get("event_type") or ""))}</td>
                <td>{html.escape(str(record.get("instrument_key") or ""))}</td>
                <td>{html.escape(str(record.get("cross_type") or ""))}</td>
                <td>{html.escape(str(record.get("suggested_order_side") or ""))}</td>
                <td>{html.escape(str(record.get("nearest_instruments_count") or 0))}</td>
                <td>{html.escape(str(record.get("budget_instruments_count") or 0))}</td>
            </tr>
            """)
    telegram_table_content = ''.join(telegram_rows) if telegram_rows else '<tr><td colspan="5">No local Telegram messages received.</td></tr>'
    algo_table_content = ''.join(algo_rows) if algo_rows else '<tr><td colspan="8">No local Algo App events received.</td></tr>'
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="refresh" content="10">
        <title>{APP_TITLE}</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ margin:0; padding:24px; background:#f4f7fb; color:#18212f; font-family:Arial,Helvetica,sans-serif; }}
            .container {{ width:100%; max-width:1500px; margin:0 auto; }}
            .header {{ background:#172554; color:white; border-radius:16px; padding:24px; margin-bottom:20px; box-shadow:0 8px 24px rgba(23,37,84,0.18); }}
            .header h1 {{ margin:0 0 8px 0; font-size:28px; }}
            .header p {{ margin:0; color:#dbeafe; }}
            .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:20px; }}
            .metric {{ background:white; border-radius:14px; padding:18px; box-shadow:0 4px 14px rgba(15,23,42,0.08); }}
            .metric-label {{ color:#64748b; font-size:13px; text-transform:uppercase; letter-spacing:0.05em; }}
            .metric-value {{ margin-top:8px; font-size:28px; font-weight:700; color:#0f172a; }}
            .section {{ background:white; border-radius:14px; padding:20px; margin-bottom:20px; box-shadow:0 4px 14px rgba(15,23,42,0.08); overflow-x:auto; }}
            .section h2 {{ margin:0 0 16px 0; font-size:20px; }}
            table {{ width:100%; border-collapse:collapse; min-width:900px; }}
            th,td {{ padding:12px; border-bottom:1px solid #e2e8f0; text-align:left; vertical-align:top; font-size:13px; }}
            th {{ background:#f8fafc; color:#334155; font-weight:700; }}
            tr:hover td {{ background:#f8fafc; }}
            .links {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
            .links a {{ display:inline-block; padding:9px 13px; border-radius:8px; color:white; background:#2563eb; text-decoration:none; font-size:13px; }}
            .links a:hover {{ background:#1d4ed8; }}
            .status {{ display:inline-block; padding:5px 9px; border-radius:999px; background:#dcfce7; color:#166534; font-size:12px; font-weight:700; }}
            .footer {{ color:#64748b; text-align:center; font-size:12px; padding:8px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <span class="status">LOCAL TEST MODE</span>
                <h1>{APP_TITLE}</h1>
                <p>Receives local Telegram notifications and local Algo App EMA payloads. Dashboard refreshes every 10 seconds.</p>
                <div class="links">
                    <a href="docs">Swagger API</a>
                    <a href="health">Health</a>
                    <a href="status">Status</a>
                    <a href="api/telegram/messages">Telegram JSON</a>
                    <a href="api/algo/events">Algo JSON</a>
                </div>
            </div>
            <div class="metrics">
                <div class="metric"><div class="metric-label">Telegram Messages</div><div class="metric-value">{get_telegram_message_count()}</div></div>
                <div class="metric"><div class="metric-label">Algo Events</div><div class="metric-value">{get_algo_event_count()}</div></div>
                <div class="metric"><div class="metric-label">Maximum Records</div><div class="metric-value">{MAX_RECORDS_PER_FILE}</div></div>
                <div class="metric"><div class="metric-label">Service Port</div><div class="metric-value">{PORT}</div></div>
            </div>
            <div class="section">
                <h2>Local Telegram Messages</h2>
                <table>
                    <thead><tr><th>Received At</th><th>Message ID</th><th>Level</th><th>Title</th><th>Message</th></tr></thead>
                    <tbody>{telegram_table_content}</tbody>
                </table>
            </div>
            <div class="section">
                <h2>Local Algo App Events</h2>
                <table>
                    <thead><tr><th>Received At</th><th>Event ID</th><th>Event Type</th><th>Instrument</th><th>Cross Type</th><th>Suggested Side</th><th>Nearest</th><th>Budget</th></tr></thead>
                    <tbody>{algo_table_content}</tbody>
                </table>
            </div>
            <div class="footer">Version {APP_VERSION} | http://{HOST}:{PORT}</div>
        </div>
    </body>
    </html>
    """

@app.on_event("startup")
async def startup_event() -> None:
    ensure_storage()

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(content=get_dashboard_html(), status_code=200)

@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "local_tele_algo_app",
        "title": APP_TITLE,
        "version": APP_VERSION,
        "host": HOST,
        "port": PORT,
        "telegram_endpoint": "/local-telegram/send",
        "algo_app_endpoint": "/local-algo/ema-alert",
        "telegram_message_count": get_telegram_message_count(),
        "algo_event_count": get_algo_event_count(),
        "timestamp": now_utc(),
    }

@app.get("/status")
async def service_status() -> dict:
    return {
        "status": "success",
        "service": {
            "name": "local_tele_algo_app",
            "title": APP_TITLE,
            "version": APP_VERSION,
            "host": HOST,
            "port": PORT,
            "transport_mode": "local_test",
            "production_delivery": "not_supported",
            "require_local_test_header": REQUIRE_LOCAL_TEST_HEADER,
            "expected_source_configured": bool(EXPECTED_SOURCE),
            "maximum_delay_seconds": MAX_DELAY_SECONDS,
            "default_list_limit": DEFAULT_LIST_LIMIT,
        },
        "endpoints": {
            "dashboard": "/",
            "documentation": "/docs",
            "health": "/health",
            "telegram_send": "/local-telegram/send",
            "algo_ema_alert": "/local-algo/ema-alert",
            "telegram_messages": "/api/telegram/messages",
            "algo_events": "/api/algo/events",
            "clear_all_data": "/api/test-data",
        },
        "storage": get_storage_status(),
        "timestamp": now_utc(),
    }

@app.post("/local-telegram/send", status_code=status.HTTP_200_OK)
async def receive_telegram_message(
    request: Request,
    telegram_request: TelegramRequest,
    fail: bool = Query(default=False),
    delay_seconds: float = Query(default=0.0, ge=0.0),
    status_code_value: int | None = Query(default=None, alias="status_code"),
) -> dict:
    validate_local_request(request)
    await apply_simulated_delay(delay_seconds)
    if fail or status_code_value is not None:
        failure_status_code = normalize_status_code(status_code_value, default_status_code=500)
        raise HTTPException(
            status_code=failure_status_code,
            detail={
                "success": False, "ok": False, "accepted": False,
                "channel": "telegram", "delivery_mode": "local_test",
                "error": "Simulated local Telegram delivery failure.",
                "timestamp": now_utc(),
            },
        )
    payload = make_json_safe(telegram_request.model_dump(mode="json"))
    message_id = build_message_id()
    record = {
        "message_id": message_id,
        "received_at": get_utc_timestamp(),
        "channel": "telegram",
        "delivery_mode": "local_test",
        "source": payload.get("source", "option_feed_engine_local_test"),
        "title": payload.get("title"),
        "level": payload.get("level"),
        "context": payload.get("context"),
        "message_preview": truncate_text(strip_html_tags(payload.get("message")), maximum_length=300),
        "request_metadata": get_request_metadata(request),
        "payload": payload,
    }
    saved_record = save_telegram_message(record)
    return {
        "success": True, "ok": True, "accepted": True,
        "channel": "telegram", "delivery_mode": "local_test",
        "message_id": message_id,
        "received_at": saved_record.get("received_at"),
        "result": {"message_id": message_id, "accepted": True, "stored": True},
    }

@app.post("/local-algo/ema-alert", status_code=status.HTTP_200_OK)
async def receive_algo_ema_alert(
    request: Request,
    payload: dict = Body(...),
    fail: bool = Query(default=False),
    delay_seconds: float = Query(default=0.0, ge=0.0),
    status_code_value: int | None = Query(default=None, alias="status_code"),
) -> dict:
    validate_local_request(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    if not payload:
        raise HTTPException(status_code=400, detail="Request body must not be empty.")
    await apply_simulated_delay(delay_seconds)
    event_id = extract_event_id(payload, request)
    if fail or status_code_value is not None:
        failure_status_code = normalize_status_code(status_code_value, default_status_code=500)
        raise HTTPException(
            status_code=failure_status_code,
            detail={
                "success": False, "ok": False, "accepted": False,
                "event_id": event_id, "channel": "algo_app", "delivery_mode": "local_test",
                "error": "Simulated local Algo App delivery failure.",
                "timestamp": now_utc(),
            },
        )
    safe_payload = make_json_safe(payload)
    event_type = str(safe_payload.get("event_type") or "unknown_event").strip()
    instrument_key = extract_instrument_key(safe_payload)
    cross_type = extract_cross_type(safe_payload)
    suggested_order_side = extract_suggested_side(safe_payload)
    nearest_count, budget_count = get_payload_counts(safe_payload)
    record = {
        "event_id": event_id,
        "received_at": get_utc_timestamp(),
        "channel": "algo_app",
        "delivery_mode": "local_test",
        "event_type": event_type,
        "source": safe_payload.get("source"),
        "schema_version": safe_payload.get("schema_version"),
        "instrument_key": instrument_key,
        "cross_type": cross_type,
        "suggested_order_side": suggested_order_side,
        "nearest_instruments_count": nearest_count,
        "budget_instruments_count": budget_count,
        "request_metadata": get_request_metadata(request),
        "payload": safe_payload,
    }
    saved_record = save_algo_event(record)
    return {
        "success": True, "ok": True, "accepted": True, "stored": True,
        "event_id": event_id,
        "event_type": event_type,
        "channel": "algo_app",
        "delivery_mode": "local_test",
        "instrument_key": instrument_key,
        "cross_type": cross_type,
        "suggested_order_side": suggested_order_side,
        "nearest_instruments_count": nearest_count,
        "budget_instruments_count": budget_count,
        "received_at": saved_record.get("received_at"),
        "message": "Local Algo App accepted and stored the event.",
    }

@app.get("/api/telegram/messages")
async def list_telegram_messages(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_RECORDS_PER_FILE),
    newest_first: bool = Query(default=True),
) -> dict:
    messages = get_telegram_messages(limit=limit, newest_first=newest_first)
    return {
        "status": "success",
        "channel": "telegram",
        "total_count": get_telegram_message_count(),
        "returned_count": len(messages),
        "limit": limit,
        "newest_first": newest_first,
        "messages": messages,
    }

@app.get("/api/telegram/messages/{message_id}")
async def get_single_telegram_message(message_id: str) -> dict:
    record = get_telegram_message(message_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Local Telegram message not found: {message_id}")
    return {"status": "success", "message": record}

@app.delete("/api/telegram/messages/{message_id}")
async def remove_telegram_message(message_id: str) -> dict:
    deleted = delete_telegram_message(message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Local Telegram message not found: {message_id}")
    return {"status": "success", "deleted": True, "message_id": message_id}

@app.delete("/api/telegram/messages")
async def remove_all_telegram_messages() -> dict:
    previous_count = get_telegram_message_count()
    clear_telegram_messages()
    return {"status": "success", "deleted_count": previous_count, "remaining_count": 0, "channel": "telegram"}

@app.get("/api/algo/events")
async def list_algo_events(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_RECORDS_PER_FILE),
    newest_first: bool = Query(default=True),
) -> dict:
    events = get_algo_events(limit=limit, newest_first=newest_first)
    return {
        "status": "success",
        "channel": "algo_app",
        "total_count": get_algo_event_count(),
        "returned_count": len(events),
        "limit": limit,
        "newest_first": newest_first,
        "events": events,
    }

@app.get("/api/algo/events/{event_id}")
async def get_single_algo_event(event_id: str) -> dict:
    record = get_algo_event(event_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Local Algo App event not found: {event_id}")
    return {"status": "success", "event": record}

@app.delete("/api/algo/events/{event_id}")
async def remove_algo_event(event_id: str) -> dict:
    deleted = delete_algo_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Local Algo App event not found: {event_id}")
    return {"status": "success", "deleted": True, "event_id": event_id}

@app.delete("/api/algo/events")
async def remove_all_algo_events() -> dict:
    previous_count = get_algo_event_count()
    clear_algo_events()
    return {"status": "success", "deleted_count": previous_count, "remaining_count": 0, "channel": "algo_app"}

@app.get("/api/storage")
async def storage_status() -> dict:
    return {"status": "success", "storage": get_storage_status()}

@app.delete("/api/test-data")
async def clear_test_data() -> dict:
    telegram_count = get_telegram_message_count()
    algo_count = get_algo_event_count()
    clear_all_records()
    return {
        "status": "success",
        "message": "Local alert test data cleared.",
        "deleted": {"telegram_messages": telegram_count, "algo_events": algo_count, "total": telegram_count + algo_count},
        "remaining": {"telegram_messages": 0, "algo_events": 0},
        "timestamp": now_utc(),
    }

@app.post("/api/test/telegram")
async def create_test_telegram_message(request: Request) -> dict:
    test_payload = TelegramRequest(
        channel="telegram",
        delivery_mode="local_test",
        source="local_simulator_test",
        title="Local Telegram Test",
        level="INFO",
        context="local_simulator_manual_test",
        message="This is a manually generated local Telegram test message.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        market_time=now_utc(),
        metadata={"manual_test": True},
    )
    return await receive_telegram_message(
        request=request,
        telegram_request=test_payload,
        fail=False,
        delay_seconds=0.0,
        status_code_value=None,
    )

@app.post("/api/test/algo")
async def create_test_algo_event(request: Request) -> dict:
    test_payload = {
        "schema_version": "1.0",
        "source": "local_simulator_test",
        "event_id": build_event_id(),
        "event_type": "isolated_instrument_ema_alert",
        "instrument": {
            "instrument_key": "TEST_NSE_FO|24500CE",
            "instrument_type": "CE",
            "strike_price": 24500.0,
            "trading_symbol": "NIFTY 24500 CE LOCAL TEST",
            "live_ltp": 120.5,
        },
        "ema": {
            "cross_type": "bullish_cross",
            "current_signal": "bullish",
            "calculation_mode": "candle_close",
            "ema_fast": 121.25,
            "ema_slow": 120.95,
            "candle": {
                "timestamp": now_utc(),
                "open": 118.0,
                "high": 123.0,
                "low": 117.5,
                "close": 120.5,
                "volume": 250000,
            },
        },
        "opening_range": {"selected_level": "R3", "available": True},
        "order_suggestion": {
            "suggested_order_side": "CE",
            "nearest_instruments": [],
            "budget_filter": {"minimum_price": 50.0, "maximum_price": 120.0, "matched_count": 0, "instruments": []},
        },
        "local_test": {"enabled": True},
    }
    return await receive_algo_ema_alert(
        request=request,
        payload=test_payload,
        fail=False,
        delay_seconds=0.0,
        status_code_value=None,
    )

@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("local_tele_algo_app.main:app", host=HOST, port=PORT, reload=True, log_level="info")