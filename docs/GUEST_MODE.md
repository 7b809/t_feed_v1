# Guest Mode (Read-only Charts)

Guest Mode allows an unauthenticated visitor to open a shared instrument chart and search instruments without creating an account.

## Enable / disable

```dotenv
GUEST_MODE_ENABLED=true
UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES=30
```

Set `GUEST_MODE_ENABLED=false` to require authentication for chart and search pages again. Restart the application after changing this setting. Keep `PROD_MODE=true` in Railway. The existing application intentionally disables its authentication middleware when `PROD_MODE=false` for local development; do not expose that mode publicly.

## Guest allow-list

Guests may use only these surfaces:

- `GET /charts` — instrument search page.
- `GET /chart/{instrument_key}` — one instrument chart.
- `GET /api/upstox/search` — instrument search.
- `GET /api/upstox/instrument` — exact instrument metadata lookup.
- `GET /api/upstox/intraday` and `GET /api/upstox/historical` — single-instrument candle data.
- `GET /api/upstox/subscription-status` — read-only subscription status.
- A chart WebSocket with exactly one `instrument_key` on `/ws/live`, `/ws/chart`, or `/all-feeds`.

Guest WebSocket connections cannot request all instruments, multiple keys, selector-based broad subscriptions, or list-feeds. The chart's explicit subscribe-to-feed control is hidden for guests.

## Not available to guests

All other endpoints remain behind the existing authentication guard. This includes admin and account management, member-only pages, order and position APIs, sandbox/broker execution, isolation controls, POST subscription changes, multi-instrument candle endpoints, token details, and operational diagnostics. The external isolation sender's exact `POST /api/isolation` webhook remains reachable because it independently validates `X-Isolation-Key`; its GET read/list endpoints are authenticated.

## Token cache refresh

The application primes its in-process Upstox token cache during startup, then reads the configured MongoDB token document every `UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES` (default 30 minutes). If the access token changes, the cache is atomically replaced and the live feed is restarted so its next connection uses the new token.

A MongoDB read failure or an empty token document does not erase the last known-good cached token. Token values are never written to logs.

## Operational recommendations

1. Keep the MongoDB token collection read-only from this application; the upstream token-management workflow should remain the only writer.
2. Put a CDN/WAF or reverse-proxy rate limit in front of public search and candle endpoints. Suggested starting limits: 30 search requests/minute/IP and 90 candle requests/minute/IP, adjusted to real usage.
3. Set a maximum request/connection rate at the edge for WebSockets, and monitor Upstox API quota and 401/429 responses.
4. Keep guest mode read-only. Do not expose account creation shortcuts, order routes, or broker credentials through guest APIs.
5. For Railway deployments with multiple replicas, remember that token refresh and feed reconnect run per process. Ensure the deployment topology intentionally supports one live-feed owner, or coordinate feed ownership externally to avoid duplicate upstream connections.

## Verification checklist

- Open a shared chart URL in a private/incognito window.
- Search for an instrument and open the result.
- Confirm historical candles load and the chart WebSocket reports a single-instrument subscription.
- Confirm `POST /api/upstox/subscribe`, `/api/upstox/historical/multi`, `/admin/*`, and order APIs are denied without authentication.
- Replace the MongoDB access token and confirm that the process logs a token-change event (never the token itself) and reconnects the feed on the next refresh interval.
