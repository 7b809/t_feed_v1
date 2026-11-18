# Upstox Order Management Service

A standalone FastAPI order service added alongside the existing feed/EMA application.

## Features
- Official Upstox Python SDK adapter (V3 order placement by default; V2 configurable).
- Defaults: BUY, quantity 65, MARKET, intraday product `I`, DAY validity.
- Single-position exit: finds a completed order and matching current net position, then submits an opposite-side MARKET order for up to that order's filled quantity, bounded by the current net position.
- Exit all positions through Upstox's official exit-positions API, optionally filtered by segment or tag.
- Local event journal at `data/YYYY-MM-DD/*.json`.
- MongoDB daily document at `UPSTOX_APP_V2.sandbox_orders`: `_id` and `date` are YYYY-MM-DD, `orders` is an array, plus `created_time` and `last_modified_time`.

## Safety
No orders are sent on startup. Live order execution is disabled unless `TRADING_ENABLED=true`. Place, order-book, and exit APIs additionally require the `X-Order-API-Key` header. Set a strong `ORDER_API_KEY` value in `.env`; protected endpoints remain disabled if it is missing. Keep the trading switch false during setup.
A MARKET order is not guaranteed to fill at the last traded/displayed price. Upstox expects `price=0` for MARKET orders. Validate instrument token, F&O lot-size multiples, funds, exchange session and market protection before enabling live execution. Keep the service bound to localhost or behind a trusted reverse proxy/firewall.

## Setup
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-orders.txt
# copy .env.example .env (Windows: copy .env.example .env)
```
Set `ORDER_API_KEY`, `UPSTOX_ACCESS_TOKEN` and `MONGO_URL` in `.env`. The default database and collection are `UPSTOX_APP_V2` and `sandbox_orders`.

Run:
```bash
uvicorn app.order_main:app --host 127.0.0.1 --port 8010
```
Swagger UI: `http://127.0.0.1:8010/docs`.

## Endpoints
- `GET /health`
- `GET /api/orders/health`
- `POST /api/orders/place` (requires X-Order-API-Key)
- `GET /api/orders/book` (requires X-Order-API-Key)
- `POST /api/orders/exit/single` (requires X-Order-API-Key)
- `POST /api/orders/exit/all` (requires X-Order-API-Key)

Example:
```bash
curl -X POST http://127.0.0.1:8010/api/orders/place ^
  -H "Content-Type: application/json" ^
  -H "X-Order-API-Key: YOUR_SECRET_KEY" ^
  -d "{\"instrument_token\":\"NSE_EQ|INE669E01016\"}"
```

Example placement body (defaults may be omitted):
```json
{
  "instrument_token": "NSE_EQ|INE669E01016",
  "transaction_type": "BUY",
  "quantity": 65,
  "product": "I",
  "order_type": "MARKET",
  "validity": "DAY",
  "price": 0
}
```

## Mongo document example
```json
{
  "_id": "2026-11-18",
  "date": "2026-11-18",
  "created_time": "...",
  "last_modified_time": "...",
  "orders": [
    {
      "event_id": "...",
      "event_type": "ORDER_PLACEMENT",
      "status": "placed",
      "request": {},
      "response": {},
      "created_time": "...",
      "last_modified_time": "..."
    }
  ]
}
```

Local JSON and MongoDB writes are attempted independently. Each event includes `local_persisted` and `mongo_persisted`; any store failure is returned as an error field and logged. The local event is rewritten with both persistence outcomes when the local filesystem is available.

## Single-exit behavior and limitation
Upstox's official exit-positions endpoint exits open positions with optional segment/tag filters; it is not a close-by-order-ID endpoint. The single-exit endpoint therefore uses the selected order ID to find a completed order, reads its filled quantity, checks the current net position for the same instrument/product, and submits the opposite-side market order for the smaller of the selected order's filled quantity and current net quantity. It refuses to reverse the current net position. If multiple orders exist in the same instrument/product, exact attribution to a single order is inherently limited by net-position accounting; verify the order book and positions after the exit request.
