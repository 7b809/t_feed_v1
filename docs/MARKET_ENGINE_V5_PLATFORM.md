# Market Engine V5 — Platform Accounts

V5 is based on `market-engine-v4`. Trading/feed/strategy services are retained; this branch adds a separate member identity layer.

## Identity storage

Member identity is stored separately from Upstox tokens and trading state:

- MongoDB database: `MARKET_ENGINE_IDENTITY` (configurable)
- `users` collection: profile, password hash, role, account status, timestamps and session version
- `auth_audit` collection: registration and authentication events

Set `USER_AUTH_MONGO_DB`, `USER_AUTH_USERS_COLLECTION` and `USER_AUTH_AUDIT_COLLECTION` to override names. The Mongo connection reuses `MONGO_URL` / `MONGO_URI` from the existing deployment. Do not use the trading token collection for member accounts.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with per-user random salts. Plain-text passwords are never stored. New registrations are `pending`; an administrator must approve before sign-in is enabled. Account status changes increment a session version, invalidating existing member sessions.

## Required environment

```dotenv
USER_AUTH_MONGO_DB=MARKET_ENGINE_IDENTITY
USER_AUTH_USERS_COLLECTION=users
USER_AUTH_AUDIT_COLLECTION=auth_audit
USER_AUTH_COOKIE_NAME=market_member
USER_AUTH_SESSION_TTL_SECONDS=28800
USER_AUTH_SESSION_SECRET=<generate-a-unique-random-secret-at-least-32-characters>
```

Generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Use a unique value for V5 member sessions; do not reuse the Upstox token or database password. Set `ADMIN_SESSION_SECRET` separately for the existing administrator login.

## Account routes

- `GET /register`, `POST /register`: member registration
- `GET /user/login`, `POST /user/login`: member sign-in
- `POST /user/logout`: member sign-out
- `GET /user/me`: current member identity
- `GET /admin/users?status=pending`: list member accounts
- `POST /admin/users/{user_id}/approve`: approve
- `POST /admin/users/{user_id}/suspend`: suspend and invalidate sessions
- `POST /admin/users/{user_id}/reject`: reject and invalidate sessions
- `POST /admin/users/{user_id}/revoke-sessions`: revoke existing sessions

The `/admin/users` endpoints are protected by the existing V4 administrator authentication gate. Keep `PROD_MODE=true` in production.

## Member access policy

Member sessions are allow-listed for the home and charts pages, chart pages, live read-only market endpoints and chart WebSockets. All other routes are denied with HTTP 403. The existing administrator authentication path retains V4 access.

This is a first platform-access layer, not a complete public SaaS launch checklist. Before opening public registration broadly, add email verification, rate limiting / abuse controls, CSRF protection for cookie-authenticated state-changing forms, password-reset flow, privacy/retention policy, backup/restore for the identity database, and monitoring/alerting. WebSocket and every newly added route must remain covered by an explicit role policy. Do not expose admin credentials or session secrets to users.


## V5 platform operating profile

V5 focuses on live market charts, live feed, instrument isolation, external EMA V2 S/R, strategy signals, dummy-order lifecycle and the sandbox/order-placement interface.

V5 disables all outbound Telegram messages (including isolation, SR confirmations, dummy-order, sandbox, startup/shutdown and readiness notifications), the Telegram token-management polling bot, scheduled MEGA archive backups, and the daily-backup admin API.

V5 retains MongoDB historical-candle extraction used by the existing startup market-data pipeline, daily market-data refresh, first-candle calculation, level-isolation recovery, live feed, external SR WebSocket, isolation and order-processing logic. These are operational inputs for chart history and strategy processing, not the daily archive/upload feature.

Logging defaults to application lifecycle INFO from `main.py`, plus WARNING/ERROR/CRITICAL from every module. Set `V5_QUIET_LOGS=false` temporarily for detailed troubleshooting.


## MongoDB separation and V4 Upstox token compatibility

V5 uses the same MongoDB connection URI but separates database responsibilities:

| Purpose | Database | Collection |
| --- | --- | --- |
| Legacy shared Upstox credential document (V4-compatible) | `UPSTOX_APP_V2` | `auth_tokens` |
| V5 operational market data | `market_data` (configurable via `MONGO_DB`) | Feature-specific collections |
| Member identity and access audit | `MARKET_ENGINE_IDENTITY` | `users`, `auth_audit` |

Both `services/token_service.py` (live-feed connection) and `token_tasks/token_store.py` (token validation/status) use the same V4-compatible document contract: `_id = upstox_access_token`, with the `access_token` field. Token validation metadata is written back to this same document. The raw token is never written to application logs.

Required environment settings:

```dotenv
MONGO_URL=<same MongoDB connection URI used by V4>
MONGO_DB=market_data
UPSTOX_TOKEN_MONGO_DB=UPSTOX_APP_V2
TOKENS_COLLECTION=auth_tokens
UPSTOX_TOKEN_DOC_ID=upstox_access_token
```

Do not copy the token into the V5 member database. Member credentials and audit events remain in `MARKET_ENGINE_IDENTITY`; the Upstox access token remains in the existing V4-compatible store.
