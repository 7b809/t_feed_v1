# sandbox_v1 — Upstox Order Service

A dedicated FastAPI order service using the official Upstox Python SDK. It follows the repository's isolation-service conventions: centralized environment configuration, a shared MongoDB connection, token loading, module logging, API routes, services and repositories.

## Defaults and safety

- API port: **8004**
- MongoDB database: **UPSTOX_APP_V2**
- Token source: **app_tokens**, document **_id=upstox_access_token**, field **access_token**
- Daily order collection: **sandbox_orders**
- Default order: **BUY**, quantity **65**, **MARKET**, product **I (Intraday)**, validity **DAY**
- Upstox sandbox mode: enabled by default
- Order execution: disabled by default

The service does not submit orders until TRADING_ENABLED=true is explicitly configured. Keep UPSTOX_SANDBOX=true and use a sandbox access token for initial tests. To switch to a live account, both the live environment selection and trading enable flag must be changed deliberately.

## Install and run

1. Copy .env.example to .env.
2. Set MONGO_URI and ensure the UPSTOX_APP_V2.app_tokens document is available.
3. Install dependencies using: pip install -r requirements.txt.
4. Run ./start.sh on Linux or run.bat on Windows.

The application starts with Uvicorn on port 8004. Swagger UI is available at /docs; health is available at /health.

### IP binding

By default, ENABLE_IP_BINDING=false binds to 0.0.0.0. Set ENABLE_IP_BINDING=true and BIND_IP=127.0.0.1 (or another assigned interface address) to bind to a specific IP. API_PORT defaults to 8004.

## API

### Place an order

POST /api/v1/orders/place

Example body (instrument key must be valid for the account):

    {
      "instrument_token": "NSE_FO|43919"
    }

The defaults are BUY, 65, MARKET and Intraday. All can be overridden:

    {
      "instrument_token": "NSE_FO|43919",
      "quantity": 65,
      "transaction_type": "BUY",
      "product": "I",
      "order_type": "MARKET",
      "validity": "DAY",
      "price": 0,
      "is_amo": false,
      "slice": true
    }

### Cancel an order

POST /api/v1/orders/{order_id}/cancel

Cancellation is for an open or pending order. It is not the same as square-off of a filled position.

### Cancel multiple orders

POST /api/v1/orders/cancel/bulk

    {"order_ids": ["ORDER_ID_1", "ORDER_ID_2"]}

### Exit one order

POST /api/v1/orders/{order_id}/exit

Only an order placed and tracked by this service can be exited by ID. The service reads the broker's filled quantity and the fill state of prior exit orders, then submits an opposite-side MARKET order for the remaining quantity. If an earlier exit order is still pending, another exit is blocked to avoid duplicate square-off.

### Exit multiple selected orders

POST /api/v1/orders/exit-by-ids

    {"order_ids": ["ORDER_ID_1", "ORDER_ID_2"]}

Each order is processed independently and its result is returned in the results array.

### Exit all positions (no order IDs)

POST /api/v1/orders/exit-all

Optional filters:

    {"segment": "NSE_FO"}

With an empty body, the service invokes Upstox OrderApi.exit_positions() to square off all eligible open positions. This broker endpoint operates on positions, not individual order IDs. Upstox currently documents a maximum of 10 positions per request; confirm account and segment constraints before using it.

### Read local daily order record

- GET /api/v1/orders/today
- GET /api/v1/orders/{order_id}

## Persistence format

Each trading date has one MongoDB document in UPSTOX_APP_V2.sandbox_orders:

- _id and date: YYYY-MM-DD
- orders: array of order/action objects
- created_time
- last_modified_time

The same daily document is mirrored to data/orders/YYYY-MM-DD.json.

Entries retain broker IDs, original order parameters, exit order IDs, cancellation/exit events, broker responses and exception details. The JSON file is written atomically before MongoDB is updated. The service requires MongoDB persistence before it submits an order; if the post-response MongoDB write fails, the local JSON remains the recovery copy and the API response includes the persistence status.

## Main files

- main.py — FastAPI entry point and lifespan
- core/config.py — .env loading and settings
- core/logger.py — console, rotating log and text log handlers
- core/mongo_client.py — thread-safe PyMongo singleton
- core/token_service.py — reads Upstox token from MongoDB
- api/routes/orders.py — order HTTP endpoints
- models/order_models.py — request validation
- services/order_service.py — Upstox SDK operations and lifecycle rules
- repositories/order_repository.py — daily JSON and MongoDB persistence

## Important trading notes

- MARKET orders use price 0; execution price is determined by the exchange/broker.
- F&O quantity must satisfy the instrument lot-size and exchange rules. The default quantity of 65 is a configurable starting default, not a guarantee that 65 is valid for every instrument.
- ID-specific exits are limited to orders tracked by this service so that the application can account for its own exit orders. Other positions can be squared off through the broker's exit-all-positions endpoint.
- A broker order response means the order was submitted/accepted; it does not necessarily mean the order is fully filled. Check the order status before treating it as executed.
