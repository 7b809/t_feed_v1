# Upstox Order Management Service

A standalone FastAPI order service added alongside the existing feed/EMA application.

## Features
- Official Upstox Python SDK adapter (V3 order placement by default; V2 configurable).
- Defaults: BUY, quantity 65, MARKET, intraday product `I`, DAY validity.
- Single-position exit: finds the completed order and matching current net position, then submits an opposite-side MARKET order.
- Exit all positions through Upstox's official exit-positions API, optionally filtered by segment or tag.
- Local event journal at `data/YYYY-MM-DD/*.json`.
- MongoDB daily document at `UPSTOX_APP_V2.sandbox_orders`: `_id` and `date` are YYYY-MM-DD, `orders` is an array, plus `created_time` and `last_modified_time`.

## Safety
No orders are sent on startup. Live order execution is disabled unless `TRADING_ENABLED=true`. Keep it false during setup. A MARKET order is not guaranteed to fill at the last traded/displayed price. Validate instrument token, F&O lot-size multiples, funds, exchange session and market protection before enabling live execution.

## Setup
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-orders.txt
# copy .env.example .env (Windows: copy .env.example .env)
```
Set `UPSTOX_ACCESS_TOKEN` and `MONGO_URL` in `.env`. The default database and collection are already set to `UPSTOX_APP_V2` and `sandbox_orders`.

Run:
```bash
uvicorn app.order_main:app --host 127.0.0.1 --port 8010
```
Swagger UI: `http://127.0.0.1:8010/docs`.

## Endpoints
- `GET /health`
- `GET /api/orders/health`
- `POST /api/orders/place`
- `GET /api/orders/book`
- `POST /api/orders/exit/single`
- `POST /api/orders/exit/all`

Example place request:
```json
{
  "instrument_token": "NSE_EQ|INE669E01016",
  "transaction_type": "BUY",
  "quantity": 65,
  "product": "I",
  "order_type": "MARKET",
  "validity": "DAY"
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

Local JSON is written before MongoDB persistence. If MongoDB is unavailable the local journal is retained and the API reports the persistence error rather than indicating both writes succeeded.

## Single-exit behavior
Upstox's official exit-positions endpoint exits open positions and supports segment/tag filtering; it is not an arbitrary order-id close endpoint. The single-exit endpoint therefore uses order ID to find a completed order, maps it to the current net position by instrument and product, and sends the opposite-side market order for the full current net quantity. Multiple fills/orders in the same instrument/product make exact attribution ambiguous, so verify the order book and positions afterward.
