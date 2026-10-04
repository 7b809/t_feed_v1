"""
Instrument Search service package.

Wraps Upstox `GET /v2/instruments/search` — search instruments by free text
(symbol, name, ISIN, strike) with exchange/segment/expiry/ATM-offset filters.
"""

from upstox_app.instruments_search.router import router as instruments_search_router

__all__ = ["instruments_search_router"]