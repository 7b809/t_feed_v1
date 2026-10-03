"""
api/__init__.py

Single entry point that aggregates every global API router.
Register this in main.py with:

    from api import api_router
    app.include_router(api_router)
"""
from fastapi import APIRouter

from api.instruments_api import router as instruments_router

api_router = APIRouter()
api_router.include_router(instruments_router)

__all__ = ["api_router"]