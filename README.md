
# ============================================================
# APPLICATION STARTUP / DAILY MARKET INITIALIZATION
# ============================================================
#
# This comment block documents the expected lifecycle for testing,
# debugging, maintenance, and future feature work.
#
# APPLICATION STARTUP:
#
# 1. Refresh Upstox token from MongoDB.
#
# 2. Fetch latest option contracts.
#
# 3. Apply configured strike range and rebuild subscriptions.
#
# 4. Fetch historical candles for all subscribed instruments.
#
# 5. Calculate historical EMA crossover state.
#
# 6. Initialize live EMA state.
#
# 7. Check the current market date/time.
#
# 8. Check whether today's configured Opening Range fetch time has passed.
#
# 9. If the application starts BEFORE the Opening Range scheduled time:
#    - Do not fetch Opening Range immediately.
#    - Wait for the normal scheduled 09:18 AM job.
#
# 10. If the application starts AFTER the Opening Range scheduled time:
#     - Check the saved Opening Range result file.
#     - Verify that today's date is present.
#     - Verify that the saved result status is successful.
#
# 11. If today's Opening Range result is NOT available:
#     - Automatically run the Opening Range fetch during startup.
#     - Do not wait for the next trading day's 09:18 AM job.
#
# 12. If today's Opening Range result IS already available:
#     - Skip the startup catch-up.
#     - Avoid unnecessary duplicate Opening Range calculations.
#
# DAILY 09:00 HARD REFRESH:
#
# 13. Refresh token from MongoDB.
#
# 14. Fetch latest instruments.
#
# 15. Filter the configured strike range.
#
# 16. Rebuild the subscription cache.
#
# 17. Fetch historical candles.
#
# 18. Recalculate EMA crossover state.
#
# 19. Initialize live EMA state.
#
# 20. Restart the Upstox streamer with refreshed subscriptions.
#
# DAILY 09:18 OPENING RANGE:
#
# 21. Read subscribed instruments.
#
# 22. Fetch today's intraday candles.
#
# 23. Select the configured Opening Range candles.
#
# 24. Calculate Opening Range OHLC and average.
#
# 25. Calculate R1/S1, R2/S2, R3/S3 and thresholds.
#
# 26. Backfill-scan R2/R3/S2/S3 touches that occurred before the
#     Opening Range fetch.
#
# 27. Evaluate isolated instrument selection.
#
# 28. Save Opening Range results.
#
# 29. Update the in-memory Opening Range cache.
#
# 30. Make Opening Range levels available for EMA WebSocket enrichment.
#
# LIVE PROCESSING:
#
# 31. Continue live Upstox tick processing.
#
# 32. Continue live EMA calculation for all subscribed instruments.
#
# 33. Continue live Opening Range touch monitoring after OR levels exist.
#
# 34. Keep isolated-instrument EMA Telegram alert rules active.
#
# STARTUP CATCH-UP TEST CASES:
#
# 35. Start application before 09:18 AM:
#     Expected -> no startup Opening Range fetch.
#
# 36. Start application after 09:18 AM with no today's OR result:
#     Expected -> startup Opening Range fetch runs automatically.
#
# 37. Restart application after 09:18 AM with today's OR result already saved:
#     Expected -> startup catch-up is skipped.
#
# 38. Start application on Saturday/Sunday:
#     Expected -> startup Opening Range catch-up is skipped.
#
# 39. Start application after 09:18 AM when OR result file is invalid:
#     Expected -> startup catch-up attempts a fresh Opening Range fetch.
#
# 40. If startup catch-up fails:
#     Expected -> application reports the failure through logs/Telegram
#     and the normal scheduled job remains registered for future execution.
#
# ============================================================

# if __name__ == "__main__":
#     logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
#     uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
