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
