MongoDB
   |
   | Upstox access token
   v
Token Service
   |
   v
Option Contract Service
   |
   | Filter by strike range and nearest expiry
   v
options_cache + subscribed_keys
   |
   +--------------------------+
   |                          |
   v                          v
Historical Candle API     Upstox Live WebSocket
   |                          |
   v                          v
Historical EMA State      Live ticks / live candles
                              |
                 +------------+------------+
                 |                         |
                 v                         v
          Live EMA Engine          Opening Range Engine
                 |                         |
                 | EMA crosses             | Level touches
                 |                         | R2/R3/S2/S3
                 +------------+------------+
                              |
                              v
                    Isolated Instrument Filter
                              |
                 +------------+-------------+
                 |                          |
                 v                          v
         WebSocket Broadcast       Isolated EMA Action
         all instruments                    |
                                  +---------+----------+
                                  |                    |
                                  v                    v
                              Telegram            Algo App