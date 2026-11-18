# Isolation priority, live-feed archive and daily startup cleanup

## Processing priority

The application treats the isolation decision and its persistence as the critical path:

1. Maintain the primary market feed and main-index LTPs.
2. Detect the isolation trigger and resolve the isolated and opposite option.
3. Persist the isolation to MongoDB and the local isolation archive.
4. Best-effort subscribe and archive live feed for the isolated leg, opposite leg and all four main indexes.
5. Start the SR and Supertrend capture clients.
6. Run optional option-chain discovery and capture at the lowest priority.

Steps 4–6 are explicitly isolated from the decision and persistence path. Exceptions are logged and skipped; they must not undo an isolation record or stop the primary feed, scanner or API.

## Isolated live-feed files

When an isolation is created, the capture service listens to the existing Upstox MarketFeedService rather than opening another market-data connection. It records only the isolated legs and configured main indexes:

    data/isolated/
    ├── isolated_instrument.json
    ├── live_feed/
    │   └── YYYYMMDD/
    │       ├── isolated_NSE_FO_xxxxx.jsonl
    │       ├── opposite_NSE_FO_yyyyy.jsonl
    │       ├── main_index_nifty_NSE_INDEX_Nifty_50.jsonl
    │       ├── main_index_banknifty_NSE_INDEX_Nifty_Bank.jsonl
    │       ├── main_index_midcpnifty_NSE_INDEX_NIFTY_MID_SELECT.jsonl
    │       └── main_index_sensex_BSE_INDEX_SENSEX.jsonl
    ├── sr/
    │   └── existing SR JSONL files
    └── supertrend/
        └── existing Supertrend JSONL files

Each line includes the IST receive timestamp, role, instrument key, symbol and raw Upstox payload. A bounded background writer keeps disk I/O out of the market-feed callback. If the queue fills, the service logs dropped-row counts rather than blocking the primary feed.

The listener is registered after Mongo/local isolation persistence and is removed on application shutdown. It does not unsubscribe shared main-index subscriptions.

## Option-chain isolation

Option-chain capture is optional and disabled by default. It is imported lazily by the scheduler, and each run has an exception boundary. If contract lookup, subscription or file capture fails, that cycle is logged and skipped. The isolation scan, primary feed and API continue. No option-chain exception is allowed to propagate into the isolation creation flow.

## Mandatory daily startup cleanup

By default, when the application process starts between **01:00 and 09:00 IST**, it clears the contents of these runtime directories once per IST day:

- data
- logs
- logs_text

The MEGA backup staging directory is deliberately excluded so a pending backup is not deleted. The directories themselves are retained. Current open log files are truncated in place so active file handlers remain valid; rotated and inactive log files are removed. The cleanup state file is stored in the project root and is not inside a cleanup target.

Only explicitly configured direct child directories of the project root can be targeted. Path traversal, hidden paths, symlinks and known source/config directories are rejected. Do not configure source folders, .git, virtual environments, .env files, or deployment configuration for cleanup.

The cleanup runs before the deferred-startup gate and before daily refresh, so it still runs if the market startup sequence is scheduled for 09:18. A once-per-day state marker prevents repeated cleanup on application restarts.

Configure the list in .env:

    DAILY_CLEANUP_ENABLED=true
    DAILY_CLEANUP_START_HOUR=1
    DAILY_CLEANUP_END_HOUR=9
    DAILY_CLEANUP_DIRS=data,logs,logs_text

    ISOLATED_LIVE_FEED_ENABLED=true
    ISOLATED_LIVE_FEED_QUEUE_SIZE=20000

To add another runtime folder, add its direct child folder name to DAILY_CLEANUP_DIRS. Never add a parent path or an absolute path.

## Monitoring

/health and /status expose isolated_live_feed status and the optional option_chain_capture status. The live-feed status includes queue depth and dropped rows. Review those values alongside the normal market-feed health summary when checking capture continuity.
