# Dummy order persistence and risk defaults

## Storage

Dummy entries continue to be written atomically to one local JSON file per isolation under
`DUMMY_ORDER_STATE_DIR`. They are additionally persisted to
`MONGO_DATABASE.dummy_orders`, one MongoDB document per IST date (`_id=YYYY-MM-DD`).

The daily document keeps the latest `isolated_instrument`, `orders_data.sr_order`,
and `orders_data.supertrend_order` for quick inspection. Since multiple isolations
can happen on one date, `orders_data.by_isolation` retains each isolation separately
and `orders_data.orders_history` retains an append-only order event history (capped
at 5,000 most recent events).

Mongo timestamps `created_datetime` and `last_updated_time` are BSON UTC datetimes.
The readable `orders_data.created_at`, `orders_data.last_updated_at`, and all
instrument/order event timestamps use ISO-8601 strings with timezone offsets.

## Independent flags

```env
DUMMY_ORDERS_ENABLED=true
DUMMY_SR_ORDERS_ENABLED=true
DUMMY_SUPERTREND_ORDERS_ENABLED=true
```

The two indicator flags default to true and work independently. If an indicator is
disabled, the local JSON and Mongo document record `status=DISABLED` and
`reason=orders disabled due to flag false`. The other indicator can still create
its order. The global `DUMMY_ORDERS_ENABLED=false` disables both indicators.

## Simulated BUY risk calculation

These configurable defaults apply only when a valid positive entry quote exists:

- Stop-loss: 30% below entry, i.e. `entry * 0.70`.
- Entry above ₹200: target is entry + 60 points.
- Entry from ₹100 through ₹200 inclusive: target is entry + 50 points.
- Entry below ₹100: target is entry + 40 points.

`DUMMY_TARGET_*` and `DUMMY_ORDER_STOP_LOSS_PERCENT` settings can change these
defaults without code edits. When no valid current-session entry quote is available,
the signal is still recorded, but stop-loss and target prices remain null; no price
is fabricated.

All entries remain DUMMY_ONLY. No broker order is sent.


## Order completion and excursion tracking

The shared live-feed stream monitors each open dummy BUY order on its target instrument.
It reads actual LTP fields only (not bid/ask or volume). An order completes when LTP
reaches or crosses its stop-loss or target. Completion statuses are
`COMPLETED_STOP_LOSS` and `COMPLETED_TARGET`.

Each order records start/entry time, high LTP and its timestamp, low LTP and its
timestamp, completion time, exit reason, exit price, and elapsed seconds from entry
to completion. Updates are written to the local isolation JSON and daily Mongo document.
A Telegram completion notification is sent once. Live-stream disconnections create
unobservable gaps; missed ticks cannot be reconstructed by this live monitor.
