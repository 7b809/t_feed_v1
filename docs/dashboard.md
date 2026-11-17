# OrderFlow web dashboard

The web UI uses FastAPI and Jinja2. All three pages extend the shared `templates/base.html`, which loads Bootstrap 5.3.3 and Bootstrap Icons.

- `/` — live operations overview: shared live-feed WebSocket health, subscription counts, live LTP cards, tick time, bid/ask, volume and OI.
- `/isolated` — active isolation registry: isolated and opposite contracts, contract metadata, and SR/Supertrend connection status.
- `/orders` — consolidated dummy-order ledger: summary metrics, searchable/filterable desktop table and responsive mobile order cards.

Read-only APIs:
- `GET /api/v1/dashboard/home`
- `GET /api/v1/dashboard/isolations`
- `GET /api/v1/dashboard/orders`

LTP comes from the latest persisted one-minute candle. It is not inferred from old SR or Supertrend signal prices. If no candle exists, the UI displays an unavailable price. Home refreshes every 3 seconds; the other pages refresh every 5 seconds. All times are displayed in Asia/Kolkata.

Install dependencies with `pip install -r requirements.txt`. The dashboard is monitoring-only and does not submit broker orders or change strategy execution.
