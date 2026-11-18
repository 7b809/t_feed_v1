# Live option-chain capture

Option-chain capture is controlled by `OPTION_CHAIN_CAPTURE_ENABLED` and is
**disabled by default**. When enabled, the service waits for today's
`pivot_levels` document and the primary Upstox market feed to be connected.

## Selection rules

For each configured main index (`INSTRUMENT_KEYS`):

1. Read the index's Opening Range average from `indexes[instrument_key].levels.A`.
2. Request contracts using Upstox `OptionsApi.get_option_contracts(instrument_key)`.
3. Ignore expired contracts, missing instrument keys, and non-CE/PE instruments.
4. Find the nearest expiry date that is today or later.
5. Keep both CE and PE contracts whose strike is inside the inclusive
   `average ± configured points` range.
6. Subscribe selected instrument keys in batches of 100 on the existing
   primary `MarketFeedService` connection. No separate duplicate upstream
   connection is created.

Default strike windows (points from the OR average):

| Index | Range |
|---|---:|
| NIFTY | ±300 |
| BANKNIFTY | ±500 |
| MIDCPNIFTY | ±200 |
| SENSEX | ±500 |

These are configurable. The API's returned strike ladder is authoritative:
the service does not invent or round strikes to a presumed step size.

## Output

Files are separated by index and trading date:

```text
data/option_chain/
├── option_chain_manifest_YYYYMMDD.json
├── nifty/YYYYMMDD/
│   ├── 25300_ce_NSE_FO_xxxxx.jsonl
│   ├── 25300_pe_NSE_FO_xxxxx.jsonl
│   └── ...
├── banknifty/YYYYMMDD/
├── midcpnifty/YYYYMMDD/
└── sensex/YYYYMMDD/
```

Each `.jsonl` line contains the receive timestamp, index, expiry, strike,
CE/PE side, instrument key, trading symbol, and raw Upstox feed payload.
A per-contract `.meta.json` file records the contract details and selection
range. The manifest records the pivot timestamp, selected contracts and
per-index selection ranges. The daily MEGA data backup includes these files
because they are inside `DATA_DIR`.

## Scheduling and recovery

The scheduler checks every 60 seconds. This allows option capture to start
after the primary feed and today's pivot levels become available, including
when the app starts during market hours. Successfully processed indexes
are not repeatedly queried; indexes whose contract API call failed are
retried, and incomplete subscriptions are retried. On a new IST date,
previous option subscriptions are removed and the new day's files are used.

The capture service requires `OPTION_CHAIN_FEED_MODE` to match the primary
`FEED_MODE`. Default both to `full`. If they differ, capture remains
inactive and logs the mismatch rather than silently creating another feed.

## Telegram policy

SR and Supertrend updates are written to the application logs and JSONL
files only. They do not send Telegram messages. Telegram remains reserved
for lifecycle events such as application startup/shutdown and new isolation
creation, plus genuinely actionable system errors handled by their callers.

## Environment

```env
OPTION_CHAIN_CAPTURE_ENABLED=false
OPTION_CHAIN_DATA_DIR=data/option_chain
OPTION_CHAIN_FEED_MODE=full
OPTION_CHAIN_NIFTY_RANGE_POINTS=300
OPTION_CHAIN_BANKNIFTY_RANGE_POINTS=500
OPTION_CHAIN_MIDCPNIFTY_RANGE_POINTS=200
OPTION_CHAIN_SENSEX_RANGE_POINTS=500
```
