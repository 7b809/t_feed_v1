# upstox_app/services/__init__.py
"""Service-layer modules for the Upstox app.

Each module exposes a singleton service instance that the corresponding
API router calls via ``asyncio.to_thread(...)`` so the event loop stays
responsive during Upstox network calls.
"""