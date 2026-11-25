# Multi-Interval Supertrend Feeds

## Overview

Supertrend now runs as an independent feed for every configured candle interval. Each
(instrument, interval) pair has its own candle history, calculated trend state, today's
confirmed signals, persistence file and WebSocket channel. The existing 1-minute API,
file name and channel are preserved for backward compatibility.

Default intervals: **1m, 5m, 15m and 30m**. Supported Upstox minute intervals are
1m, 2m, 3m, 5m, 10m, 15m and 30m. The configured list must include 1m.

## Local JSON files

Files are saved alongside the instrument's other indicator files:

| Interval | Filename |
|---|---|
| 1m | `supertrend.json` |
| 5m | `supertrend_5m.json` |
| 15m | `supertrend_15m.json` |
| 30m | `supertrend_30m.json` |

Example option contract directory:

```text
storage/mega/nifty/25700_CE/
    supertrend.json
    supertrend_5m.json
    supertrend_15m.json
    supertrend_30m.json
```

Index files use `storage/mega/index/{folder}/`. Writes are atomic (temporary file,
then replace). Each file contains the interval, Supertrend parameters, instrument
metadata, latest calculation state, today's IST signals and the `today_ist_date`
marker. Signals remain today-only, matching the previous implementation.

## Configuration

Set the comma-separated intervals in `.env`:

```dotenv
SUPERTREND_INTERVALS=1m,5m,15m,30m
```

The default is `1m,5m,15m,30m`. To reduce API usage, remove intervals you do not
need, but keep 1m enabled to preserve the legacy feed. Each interval uses the
existing `SUPERTREND_PERIOD`, `SUPERTREND_MULTIPLIER`,
`SUPERTREND_CHANGE_ATR`, history, worker and timeout settings.

Historical and intraday candles are requested at the configured interval. A candle
is evaluated only after its full interval has elapsed; e.g. a 15m candle is not
confirmed until 15 minutes after its open timestamp. Each interval calculates
Supertrend independently from its own OHLC candle series.

## WebSocket endpoints

The original 1m routes are unchanged:

- Index: `/ws/supertrend/index/nifty`
- Raw instrument: `/ws/supertrend/NSE_FO%7C51330`

Use the explicit interval routes for other intervals (and optionally 1m):

- Index: `/ws/supertrend/interval/15m/index/nifty`
- Raw instrument: `/ws/supertrend/interval/15m/NSE_FO%7C51330`

Replace `15m` with `5m`, `30m`, or another configured supported interval.
The WebSocket sends `connection_ack`, then a `snapshot` when the runtime has
state, followed by live `supertrend_signal` and `heartbeat` events.

### Channel names

| Interval | Channel |
|---|---|
| 1m | `supertrend:{instrument_key}` (legacy name) |
| 5m | `supertrend:{instrument_key}:5m` |
| 15m | `supertrend:{instrument_key}:15m` |
| 30m | `supertrend:{instrument_key}:30m` |

Every signal and heartbeat payload includes an `interval` field. Separate channels
ensure a 5m event can never be mistaken for a 1m event.

## REST endpoints

- `GET /api/v1/supertrend/status` — configured intervals and active instrument/interval subscriptions.
- `GET /api/v1/supertrend/{instrument_key}/latest?interval=15m` — interval snapshot.
- `POST /api/v1/supertrend/subscribe/{instrument_key}?interval=15m` — prewarm/subscribe one interval.
- `POST /api/v1/supertrend/unsubscribe/{instrument_key}?interval=15m` — remove one interval.
- `POST /api/v1/supertrend/unsubscribe/{instrument_key}` — remove all intervals for that instrument.

Omitting `interval` from latest/subscribe retains the legacy default `1m`.

## Processing lifecycle

1. At startup, each configured index is subscribed for every configured interval.
2. Each pair is warmed using interval-matched historical and current-day candles.
3. Historical and intraday candles are merged and de-duplicated by timestamp.
4. The currently forming candle is excluded using that interval's duration.
5. Supertrend is calculated independently; only newly detected confirmed signals are broadcast.
6. Today's signals and latest state are atomically persisted to that interval's JSON file.
7. The live loop polls all subscribed instrument/interval pairs and broadcasts on their own channels.

## Compatibility

- Existing 1m JSON remains `supertrend.json`; no `supertrend_1m.json` is created.
- Existing 1m WebSocket URLs and `supertrend:{instrument_key}` channel remain unchanged.
- Existing clients can continue using the old REST endpoints without an interval parameter.
- The shared Supertrend ATR period, multiplier and ATR method apply to all intervals.
