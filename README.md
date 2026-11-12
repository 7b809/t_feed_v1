<!-- README.md -->

# isolation_consumer_service

Downstream consumer of the isolation documents produced by `ema_v1` (app1).
Receives an isolation POST, persists it, and immediately opens two SR
WebSocket connections on app1's feed — one for the isolated option leg
and one for the opposite option leg. Persists every `snapshot`,
`sr_update`, and `heartbeat` frame; exposes read APIs; re-subscribes on
restart.

> Scope: **SR + Supertrend feed consumption**. EMA feed and broker order execution are out of scope.

---

## 1 · Startup behaviour (today-only)

On every boot the service does the following, in order:

1. Connects to MongoDB.
2. Loads `isolation_events` where
   - `consumer_status IN ("live", "subscribing")`, **and**
   - `trade_date_ist == today's IST date (YYYY-MM-DD)`.
   Also adopts app1's singleton doc (`_id = "instrument_isolation"`) if
   it is for today and `status == "ACTIVE"`.
3. **If any exist** → for each isolation it spawns the two WS clients
   (isolated + opposite) using `?config=all` — which subscribes to all
   three configs (`15/3`, `15/15`, `15/30`) in a single connection.
4. **If none exist** → it logs
   `no active isolations for today (<date>); waiting for API POST`
   and keeps running normally.

This waiting path is **the normal steady state** in **both** dev and
production. Nothing is "missing" — the process is simply idle until app1
posts an isolation.

Old isolations from previous days are **not** re-subscribed (SR is
today-only by design).

---

## 2 · `overwrite` flag

`POST /api/v1/isolation/incoming` accepts an optional `overwrite` flag:

- **Query param:** `?overwrite=true`
- **Body field:** `"overwrite": true` (stripped before persisting)

**Default: `false`.** Both dev and production honour it identically.

When `overwrite=true`:

1. **Mongo `isolation_events`** — full **replace** via `replace_one`
   (not a `$set` merge). `consumer_status` and `subscribed_legs` are
   reset to defaults.
2. **`data/instrument_isolation.json`** — the mirror file is rewritten.
3. **WS clients** — any running clients for that isolation are torn down
   and recreated from scratch (fresh `connection_ack` + snapshots).
4. **SR state files** — both legs' `sr_state/` folders are **purged**
   so fresh snapshots start from a clean slate.

When `overwrite=false` (default):

- `upsert` merges via `$set` (idempotent).
- WS layer is a **no-op** if the clients are already running.
- SR state files are left in place (snapshots will just overwrite).

Example:

```bash
curl -X POST 'http://localhost:8003/api/v1/isolation/incoming?overwrite=true' \
  -H 'X-Isolation-Key: <secret>' \
  -H 'Content-Type: application/json' \
  -d @isolation_sample.json
```

---

## 3 · Isolation POST contract

### Endpoint

```
POST /api/v1/isolation/incoming[?overwrite=true]
Header: X-Isolation-Key: <shared secret>
Content-Type: application/json
```

If `ISOLATION_NOTIFY_CLIENT_KEY` is empty in `.env`, no header is
required.

### Response (200)

```json
{
  "received": true,
  "isolation_id": "2026-11-11__NSE_FO_45406",
  "overwrite": false,
  "subscribed": ["isolated", "opposite"],
  "rejected_legs": [],
  "web_socket_sessions": {
    "NSE_FO|45406": { "state": "connecting" },
    "NSE_FO|45407": { "state": "connecting" }
  }
}
```

---

## 4 · WS endpoints consumed

```
wss://ema.novag7.in/ws/sr/{url_encoded_instrument_key}?config=all
```

Frame types: `connection_ack`, `snapshot`, `sr_update`, `heartbeat`.

Close codes:

- `1000/1001/1011` → reconnect with exponential backoff (1s → 30s).
- `4400/4404` → permanent — no retry, mark `ws_state="failed"`.

Client keepalive: `websockets` handles ping/pong automatically
(`ping_interval=20`, `ping_timeout=20`), plus a text `"ping"` sent every
`SR_WS_KEEPALIVE_SECONDS` (default 30).

---

## 5 · Storage model (what goes where)

| Data | Backend | Notes |
|---|---|---|
| Isolation documents (`isolation_events`) | **MongoDB** | `_id = trade_date__instrument_key` |
| Isolation JSON mirror | **`{DATA_DIR}/instrument_isolation.json`** | fixed name (latest-wins) |
| **SR state** | **`{SR_STATE_DIR}/{leg}__{strike}_{opt}_{key}/{config}/`** | filesystem only |
| SR heartbeat history (opt-in) | `.../\_heartbeat_history.jsonl` | `SR_STORE_HEARTBEATS=true` |

### SR state layout

For the sample isolation (`NSE_FO|45406` CE + `NSE_FO|45407` PE, strike 25400):

```text
data/
├── instrument_isolation.json
└── sr_state/
    ├── isolated__25400_ce_NSE_FO_45406/
    │   ├── 15_3/
    │   │   ├── today_snapshot.json       ← snapshot event (frozen opening)
    │   │   ├── today_live_sr.json        ← sr_update events (current live)
    │   │   └── _heartbeat_history.jsonl  ← opt-in
    │   ├── 15_15/...
    │   └── 15_30/...
    └── opposite__25400_pe_NSE_FO_45407/
        ├── 15_3/...
        ├── 15_15/...
        └── 15_30/...
```

Folder structure: **`{leg_role}__{strike}_{option_type}_{instrument_key_safe}/{config}/`**

### Two files per (instrument, config)

| File | Written by | Purpose |
|---|---|---|
| **`today_snapshot.json`** | `snapshot` event | The **opening** SR levels sent when we connected — frozen reference. `update_count = 1`. `last_event = "snapshot"`. |
| **`today_live_sr.json`** | `sr_update` event | The **current live** state after any subsequent updates. `update_count = previous + 1`. `last_event = "sr_update"`. |

After the first `sr_update`, both files exist. The snapshot stays
frozen at the connect-time prices; the live file keeps advancing as the
market moves.

`sr_update` frames do **not** carry `trading_symbol`, `left_bars`,
`right_bars`, `cycle_number`, `cycle_time`, `scope_note` — those are
**carried forward** from `today_snapshot.json` into `today_live_sr.json`
so the live file stays coherent. Same behaviour as the reference
`TodaySrSink` client (which keeps them in memory as `_last_meta`); we
persist them to disk so they survive restarts.

### Schema (identical for both files)

```json
{
  "_id":            "NSE_FO|45406__15/3",
  "instrument_key": "NSE_FO|45406",
  "leg_role":       "isolated",
  "isolation_id":   "2026-11-11__NSE_FO_45406",
  "config":         "15/3",
  "strike_option":  "25400_ce",
  "trading_symbol": "NIFTY 25400 CE 17 NOV 26",
  "left_bars":      15,
  "right_bars":     3,
  "data_scope":     "today_only",
  "today_ist_date": "2026-11-11",
  "scope_note":     "For further reference, only today's SR levels are used",
  "current_price":  197.35,
  "last_ts":        "2026-11-11T13:18:00+05:30",
  "cycle_number":   3,
  "cycle_time":     "2026-11-11T13:19:10.000895+05:30",
  "level_counts":   { "resistances": 5, "supports": 4, "total": 9 },
  "sr_level_count": 21,
  "levels":         [...full chronological pivot list...],
  "resistances":    [...5 nearest above current_price...],
  "supports":       [...5 nearest below current_price...],
  "last_event":     "snapshot" | "sr_update" | "heartbeat",
  "updated_at":     "2026-11-11T13:20:07.587491+05:30",
  "update_count":   1,
  "last_heartbeat": {
    "cycle": 4,
    "cycle_time": "...",
    "nearest_resistance": 214.6,
    "nearest_support": 193.95,
    "current_price": 197.35,
    "sr_level_count": 21,
    "status": "ok",
    "received_at": "..."
  }
}
```

### Heartbeat behaviour

`heartbeat` frames refresh only the `last_heartbeat` field on **both
existing** files. `update_count`, `last_event`, and `updated_at` are
**not** touched — they always reflect the last *level-carrying* frame.
If neither file exists yet (edge case), a minimal `today_live_sr.json`
is created with `last_event = "heartbeat"` and `update_count = 0`.

### How `strike_option` is derived

| Frame | Source |
|---|---|
| `snapshot`  | `payload.meta.strike_price` + `payload.meta.strike_type` → `"25400_ce"` |
| `sr_update` | parsed from `payload.trading_symbol` (`"NIFTY 25400 CE 17 NOV 26"` → `"25400_ce"`) |
| `heartbeat` | parsed from `payload.trading_symbol` |

Fractional strikes are preserved: `25400.5` → `25400_5`.

### Migration from previous layouts

Old files (any of these) are **not** read by the new code:

```text
data/sr_state/NSE_FO_45406__15_3.json                            (flat, pre-flatten)
data/sr_state/isolated/NSE_FO_45406__25400_ce/15_3.json          (hierarchical)
data/sr_state/isolated__25400_ce_NSE_FO_45406/15_3.json          (flattened)
data/sr_state/isolated__25400_ce_NSE_FO_45406/15_3/today_live_sr.json (single-file)
```

Delete once before deploy:

```bash
rm -rf data/sr_state/*
```

On Windows, `run.bat` already wipes `data/` on every start — auto-clean.
SR state is rebuilt from fresh WS snapshots in ~1 s after re-subscribe.

### Overwrite behaviour

`POST ...?overwrite=true` calls `delete_for_instrument_keys([iso_key,
opp_key])`, which removes the entire `*_{instrument_key}` folder
(both `isolated__...` and `opposite__...`, including all config
subfolders and their two state files) before restarting the WS clients.
The mirror `instrument_isolation.json` is rewritten with the fresh
document. On the next `snapshot` frames (≈1 s later), all config
folders are recreated with fresh `today_snapshot.json`; `today_live_sr.json`
appears as soon as the first `sr_update` lands.

---

## 6 · REST API

| Endpoint | Description |
|---|---|
| `POST /api/v1/isolation/incoming[?overwrite=true]` | receive isolation POST |
| `GET  /api/v1/isolation/current` | latest isolation document |
| `GET  /api/v1/isolation/history?limit=20` | list desc |
| `GET  /api/v1/sr/{instrument_key}/latest?config=15/3` | one config (freshest file) |
| `GET  /api/v1/sr/{instrument_key}/latest?config=all` | `{ "configs": {...} }` |
| `GET  /api/v1/sr/{instrument_key}/status` | live WS state |
| `GET  /api/v1/status` | overall health |
| `GET  /health` | liveness |

`get_latest` and `get_all_configs` return the file with the most recent
`updated_at` — normally `today_live_sr.json`; immediately after a
reconnect (fresh snapshot, no sr_update yet) it is
`today_snapshot.json`.

---

## 7 · Running

```bash
# Windows
run.bat

# Linux
./start.sh
./stop.sh
```

Direct:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8003
```

Key `.env` fields:

- `MONGO_DATABASE` / `ISOLATION_EVENTS_COLLECTION` — isolation persistence
- `SR_STATE_DIR` — filesystem folder for SR state (default `./data/sr_state`)
- `SR_STORE_HEARTBEATS` — opt-in local JSONL heartbeat log
- `DATA_DIR` — isolation JSON mirror (empty = disabled)
- `SR_WS_BASE_URL` / `SR_WS_BASE_URL_LOCAL` — upstream feed
- `ISOLATION_NOTIFY_CLIENT_KEY` — shared secret with app1
- `REQUIRE_TODAY_ONLY_SCOPE` — default `true`
- `VERBOSE` — flips per-frame logs from DEBUG to INFO

---

## 8 · Production deployment

Copy `.env.production` → `.env` and fill the three `REPLACE_WITH_*`
placeholders. Recommended settings:

```env
APP_ENV=production
MONGO_DATABASE=UPSTOX_APP_V2
ISOLATION_EVENTS_COLLECTION=project_info
SR_STATE_DIR=./data/sr_state
ISOLATION_NOTIFY_CLIENT_KEY=<matches app1>
TELEGRAM_ENABLED=true
```

`_id` coexistence inside `UPSTOX_APP_V2.project_info`:

| `_id` | Written by |
|---|---|
| `'instrument_isolation'` | app1 (untouched) |
| `'2026-11-11__NSE_FO_45406'` | consumer (one per isolation per day) |

---

## 9 · Watchdog

Every 30 s: restarts clients whose asyncio task died from network loss.
Does **not** retry clients in `failed` state (`4400` / `4404`).

---

## 10 · Verification

```bash
./start.sh
sleep 5

# Layout
tree data/sr_state /F
# → .../15_3/today_snapshot.json   (from snapshot)
# → .../15_3/today_live_sr.json    (from first sr_update)

# Live file has fresh data
jq '{last_event, update_count, current_price, left_bars, cycle_number, trading_symbol}' \
  data/sr_state/isolated__25400_ce_NSE_FO_45406/15_3/today_live_sr.json

# Snapshot file is frozen
jq '{last_event, update_count, current_price}' \
  data/sr_state/isolated__25400_ce_NSE_FO_45406/15_3/today_snapshot.json

# API returns the freshest (live)
curl -s 'http://localhost:8004/api/v1/sr/NSE_FO%7C45406/latest?config=all' | jq '.configs | keys'

# Tests
pytest -q
```

---

## 11 · Non-goals (this iteration)

- EMA feed.
- Order execution.
- Historical SR backfill.
- Per-`sr_update` Telegram notifications.

---

## 12 · Supertrend WebSocket consumption

For every ACTIVE isolation, the consumer opens **two additional independent
connections** to app1's Supertrend feed, one for each leg:

```
wss://ema.novag7.in/ws/supertrend/{url_encoded_instrument_key}
```

The SR and Supertrend connections are managed independently. A failure in
one indicator client does not stop the other indicator connection.

Supertrend frames consumed:

- `connection_ack` — records connection state.
- `snapshot` — persists the opening, today-only Supertrend state and the
  list of confirmed signals received in the initial snapshot.
- `supertrend_signal` — appends a deduplicated confirmed BUY/SELL signal.
- `heartbeat` — updates trend/ATR/last-candle health metadata without
  changing the signal list.

The client enforces `data_scope=today_only` when
`REQUIRE_TODAY_ONLY_SCOPE=true`, uses WebSocket ping/pong plus the
configured text keepalive, reconnects with exponential backoff, and does
not retry permanent close codes 4400/4404. The existing 30-second watchdog
checks both SR and Supertrend client tasks.

### Supertrend storage

Supertrend is stored separately from SR under `SUPERTREND_STATE_DIR`
(default `./data/supertrend_state`):

```text
data/
├── sr_state/
│   ├── isolated__25400_ce_NSE_FO_45406/...
│   └── opposite__25400_pe_NSE_FO_45407/...
└── supertrend_state/
    ├── isolated__25400_ce_NSE_FO_45406/
    │   ├── today_snapshot.json
    │   └── today_live_supertrend.json
    └── opposite__25400_pe_NSE_FO_45407/
        ├── today_snapshot.json
        └── today_live_supertrend.json
```

- `today_snapshot.json` is the frozen snapshot from the WebSocket
  connection, including today's confirmed signal list.
- `today_live_supertrend.json` contains the current confirmed signal
  list and most recent signal; heartbeat metadata is refreshed in place.
- Each signal is stored with the four fields supplied by app1:
  `signal`, `price`, `confirmed_time`, `confirmed_time_price`.
- Signals are deduplicated by `(signal, confirmed_time)`.
- `overwrite=true` purges both legs from both the SR and Supertrend
  state directories before fresh subscriptions start.

### Supertrend REST APIs

| Endpoint | Description |
|---|---|
| `GET /api/v1/supertrend/status` | All Supertrend client connection states |
| `GET /api/v1/supertrend/{instrument_key}/latest` | Latest saved Supertrend state |
| `GET /api/v1/supertrend/{instrument_key}/status` | One instrument's Supertrend connection state |

The combined `GET /api/v1/status` response also includes
`supertrend_active_connections` and `supertrend_connections`.

Configure the state directory:

```env
SUPERTREND_STATE_DIR=./data/supertrend_state
```

Supertrend client connection status is separate from SR status. Both
connections are started only for ACTIVE isolations and are stopped when
the isolation is closed or overwritten.
