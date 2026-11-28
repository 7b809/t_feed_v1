"""Lazy adapter to the existing MongoDB-backed Upstox token loader."""
from __future__ import annotations
from pathlib import Path

def load_access_token(env_file: Path) -> str:
    # Reuse existing token retrieval; never print or persist the token.
    from fetch_token import load_upstox_access_token
    return load_upstox_access_token(env_path=str(env_file))
