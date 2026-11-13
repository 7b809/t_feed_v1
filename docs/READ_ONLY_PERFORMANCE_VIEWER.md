# V5 Read-Only Daily Performance Viewer

## Purpose

V5 is the user-facing performance workspace. The upstream trading project remains responsible for index isolation, strategy evaluation, dummy-order creation and lifecycle updates, and writing the daily MongoDB snapshot.

V5 consumes that snapshot and presents it. In the default `PERFORMANCE_VIEWER_MODE=true` profile, V5 does not run its local isolation/backfill/dummy-order/sandbox processing pipeline.

## Source document

Default MongoDB source:

- Database: `UPSTOX_APP_V2`
- Collection: `dummy_orders`
- Document `_id`: trading date in `YYYY-MM-DD` format (India market date)
- `isolated_instrument`: isolation record, events, candidates and touch metadata
- `orders_data`: standard and POST-11:30 order records and their lifecycle
- `created_datetime`: document creation time
- `last_updated_time`: last upstream update time

The DB and collection can be changed with `PERFORMANCE_SNAPSHOT_MONGO_DB` and `PERFORMANCE_SNAPSHOT_COLLECTION`.

## Date selection and page behavior

Every API request computes today's date using `MARKET_TIMEZONE` (default `Asia/Kolkata`) and queries exactly:

```python
collection.find_one({"_id": "YYYY-MM-DD"})
```

The page does not silently fall back to yesterday's document. This applies on Saturdays, Sundays, market holidays, before market open, during market hours, and after market close.

- If today's document exists, the page displays its isolated instrument, trigger details, opening-range levels, isolation events/candidates, order lifecycle, P&L summary, source timestamps and complete JSON record.
- If today's document does not exist, the page keeps the four-index watch visible and reports that it is waiting for today's upstream document.
- If MongoDB is unavailable, the page reports that state and still attempts to display any cached live index quotes.
- The page refreshes automatically every 15 seconds and also has a manual refresh button.

## Read-only boundary

The snapshot service performs only a MongoDB `find_one` operation. It does not insert, update, replace, or delete source documents. It has no dependency on the upstream project's local JSON files.

When viewer mode is enabled, application startup disables local level isolation, dummy orders, sandbox execution, daily refresh, first-candle processing and readiness scheduling. The Upstox live feed remains available only to supply the four-index watch. It does not call the local isolation engine.

The existing API routes for administration remain separately protected by the existing authentication policy; this viewer page does not call their mutating endpoints.

## Environment

```dotenv
PERFORMANCE_VIEWER_MODE=true
PERFORMANCE_SNAPSHOT_ENABLED=true
PERFORMANCE_SNAPSHOT_MONGO_DB=UPSTOX_APP_V2
PERFORMANCE_SNAPSHOT_COLLECTION=dummy_orders
PERFORMANCE_SNAPSHOT_CONNECT_TIMEOUT_MS=4000
MARKET_TIMEZONE=Asia/Kolkata
```

The MongoDB URI must have read access to the configured source database and collection. The upstream writer must have write access independently.

## API

`GET /live/performance/today-snapshot`

Response status values:

- `found`: today's document was found and returned as `snapshot`.
- `not_found`: no document exists for today's date.
- `unavailable`: MongoDB/configuration lookup failed.
- `disabled`: snapshot reader disabled by configuration.

The response includes the computed market date, market-session classification and source database/collection. Responses are marked `Cache-Control: no-store`.

## Operational note

This is a viewer, not an order simulator or broker execution surface. P&L values are displayed from the upstream saved fields. The dashboard does not recalculate trade outcomes from candles or infer missing order transitions.


## Orders & History workspace

The `/positions` page is a read-only orders workspace, not a broker positions/action console.

- The default **Today's Orders** tab reads the daily document with `_id=YYYY-MM-DD` and normalizes `orders_data.orders`, `orders_data.order`, and `orders_data.post_1130_order` when present.
- The **Order History** tab flattens the supported order slots across daily documents in the same collection, newest trading date first.
- History pagination is server-side (10–100 rows per page) and search is applied in MongoDB before pagination. Supported search fields include trading symbol, instrument key, status, order ID and strategy/execution path.
- The page refreshes today's orders every 15 seconds while the Today tab is active. Historical results refresh on demand, page navigation, or search.
- Desktop uses a horizontally scrollable detail table; mobile uses compact order cards.

Read-only endpoints:

- `GET /live/performance/today-orders`
- `GET /live/performance/order-history?page=1&page_size=20&search=NIFTY`

Both endpoints use MongoDB read operations only. The old broker-position lock/exit APIs remain separate administrator surfaces and are not called by this workspace.
