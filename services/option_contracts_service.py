# services/option_contracts_service.py
"""Upstox option-contracts fetcher — cached, filtered wrapper.

Wraps Upstox's ``GET /v2/option/contract`` endpoint.

Reference:
    https://upstox.com/developer/api-documentation/option-contracts

Default behaviour
-----------------
``fetch()`` with no arguments returns **both CE and PE contracts** for
the **nearest weekly expiry** of ``NSE_INDEX|Nifty 50`` with strike
prices in the closed interval **``[21000, 25000]``**:

    {
        "status": "ok",
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_keyword": "current_week",
        "expiry_date": null,           # concrete date only if caller passed one
        "strike_min": 21000.0,
        "strike_max": 25000.0,
        "instrument_type": null,       # null == both CE + PE
        "count": 42,
        "contracts": [ ...raw Upstox dicts... ],
        "nearest_expiry": "2026-11-12",
        "available_expiries": ["2026-11-12", "2026-11-19", ...],
        "cache": {"hit": false, "ttl_seconds": 300, "age_seconds": 0},
        "fetched_at": "2026-11-06T09:17:10+05:30"
    }

Every input is overrideable at call time:

    * ``underlying_key``   — e.g. ``"NSE_INDEX|Nifty Bank"``
    * ``strike_min``       — inclusive lower bound (``None`` → config default)
    * ``strike_max``       — inclusive upper bound (``None`` → config default)
    * ``expiry_date``      — ``"YYYY-MM-DD"`` or a relative keyword:
                             ``current_week``, ``next_week``, ``far_week``,
                             ``current_month``, ``next_month``, ``far_month``.
                             ``None`` → config default (``current_week``).
                             ``"all"`` → no expiry filter (whole chain — large).
    * ``instrument_type``  — ``"CE"``, ``"PE"``, or ``None`` for both.
    * ``force_refresh``    — bypass the TTL cache.

Cache
-----
Results are cached per ``(underlying_key, expiry_key)`` tuple with a
default TTL of ``OPTION_CONTRACTS_CACHE_TTL_SECONDS`` (300 s).
Filtering by strike / instrument_type happens on the cached raw list,
so changing those params does **not** trigger a re-fetch. Changing the
expiry **does** — it addresses a different cache bucket.

Config keys (all overrideable via ``core/config.py``; nothing needs to
be added unless you want to change the defaults):

    OPTION_CONTRACTS_DEFAULT_UNDERLYING      "NSE_INDEX|Nifty 50"
    OPTION_CONTRACTS_DEFAULT_STRIKE_MIN      21000
    OPTION_CONTRACTS_DEFAULT_STRIKE_MAX      25000
    OPTION_CONTRACTS_DEFAULT_EXPIRY_KEYWORD  "current_week"
    OPTION_CONTRACTS_CACHE_TTL_SECONDS       300
    OPTION_CONTRACTS_HTTP_TIMEOUT_SECONDS    15
    OPTION_CONTRACTS_UPSTOX_BASE_URL         "https://api.upstox.com/v2"

Token resolution
----------------
``_resolve_access_token()`` tries, in order:

    1. ``services.upstox_auth_service.get_access_token()``   (if present)
    2. ``services.token_manager.get_access_token()``         (if present)
    3. ``token_tasks.token_monitor.get_access_token()``      (if present)
    4. ``config.UPSTOX_ACCESS_TOKEN`` / ``config.UPSTOX_TOKEN`` /
       ``config.ACCESS_TOKEN``

and raises ``OptionContractsServiceError`` when none resolve. This is
deliberate — a silent un-authenticated request would return 401 from
Upstox and look like a data bug.

See also:
    * ``routes/option_contracts_routes.py``      — HTTP surface
    * Upstox docs: https://upstox.com/developer/api-documentation/option-contracts
"""

from __future__ import annotations

import importlib
import threading
import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Config defaults (all overrideable via core/config.py)                       #
# --------------------------------------------------------------------------- #
_DEFAULT_UNDERLYING      = "NSE_INDEX|Nifty 50"
_DEFAULT_STRIKE_MIN      = 21000.0
_DEFAULT_STRIKE_MAX      = 25000.0
_DEFAULT_EXPIRY_KEYWORD  = "current_week"
_DEFAULT_CACHE_TTL       = 300
_DEFAULT_HTTP_TIMEOUT    = 15
_DEFAULT_BASE_URL        = "https://api.upstox.com/v2"

_RELATIVE_EXPIRY_KEYWORDS = frozenset({
    "current_week", "next_week", "far_week",
    "current_month", "next_month", "far_month",
})

_ALL_EXPIRY_SENTINEL = "all"


class OptionContractsServiceError(RuntimeError):
    """Raised when option-contract retrieval cannot complete."""


# --------------------------------------------------------------------------- #
# Token resolution                                                            #
# --------------------------------------------------------------------------- #
def _resolve_access_token() -> str:
    """Best-effort resolution of the Upstox access token.

    Tries several providers (SDK-agnostic) then falls back to config.
    Raises ``OptionContractsServiceError`` when nothing is found.
    """
    candidates = [
        ("services.upstox_auth_service", ("get_access_token", "access_token")),
        ("services.token_manager",       ("get_access_token", "access_token")),
        ("token_tasks.token_monitor",    ("get_access_token", "access_token")),
        ("upstox_auth",                  ("get_access_token", "access_token")),
    ]

    for mod_path, attrs in candidates:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue
        for attr in attrs:
            try:
                obj = getattr(mod, attr, None)
                if obj is None:
                    continue
                if callable(obj):
                    try:
                        val = obj()
                    except Exception:
                        continue
                    if val:
                        return str(val)
                else:
                    return str(obj)
            except Exception:
                continue

    for attr in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_TOKEN", "ACCESS_TOKEN"):
        try:
            v = getattr(config, attr, None)
            if v:
                return str(v)
        except Exception:
            continue

    raise OptionContractsServiceError(
        "No Upstox access token available — expected one of "
        "services.upstox_auth_service.get_access_token(), "
        "services.token_manager.get_access_token(), "
        "token_tasks.token_monitor.get_access_token(), or "
        "config.UPSTOX_ACCESS_TOKEN."
    )


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _now_iso() -> str:
    return datetime.now(_market_tz()).isoformat()


def _is_relative_expiry_keyword(value: str) -> bool:
    return str(value or "").strip().lower() in _RELATIVE_EXPIRY_KEYWORDS


def _normalize_expiry_input(value: str | None) -> str | None:
    """Return the canonical string to pass to Upstox for ``expiry_date``.

    Returns:
        * ``None``        → caller wants the configured default.
        * ``"all"``       → caller wants every expiry (no server filter).
        * ``"YYYY-MM-DD"`` → caller passed a concrete date.
        * keyword         → ``current_week`` / ``next_week`` / … as-is.

    Never raises — unexpected shapes are passed through verbatim so
    the upstream API can reject them with a clear message.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() == _ALL_EXPIRY_SENTINEL:
        return _ALL_EXPIRY_SENTINEL
    if _is_relative_expiry_keyword(s):
        return s.lower()
    return s  # assume YYYY-MM-DD; let Upstox validate


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class OptionContractsService:
    """Cached Upstox option-contracts fetcher + filterer."""

    def __init__(self) -> None:
        # Cache key: (underlying_key, expiry_key_or_"all")
        # Value    : (fetched_at_epoch, raw_contract_list)
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Config accessors                                                    #
    # ------------------------------------------------------------------ #
    def _cfg_default_underlying(self) -> str:
        return str(getattr(config, "OPTION_CONTRACTS_DEFAULT_UNDERLYING",
                           _DEFAULT_UNDERLYING))

    def _cfg_default_strike_min(self) -> float:
        return float(getattr(config, "OPTION_CONTRACTS_DEFAULT_STRIKE_MIN",
                             _DEFAULT_STRIKE_MIN))

    def _cfg_default_strike_max(self) -> float:
        return float(getattr(config, "OPTION_CONTRACTS_DEFAULT_STRIKE_MAX",
                             _DEFAULT_STRIKE_MAX))

    def _cfg_default_expiry_keyword(self) -> str:
        return str(getattr(config, "OPTION_CONTRACTS_DEFAULT_EXPIRY_KEYWORD",
                           _DEFAULT_EXPIRY_KEYWORD))

    def _cfg_cache_ttl(self) -> int:
        try:
            v = int(getattr(config, "OPTION_CONTRACTS_CACHE_TTL_SECONDS",
                            _DEFAULT_CACHE_TTL))
            return max(0, v)
        except Exception:
            return _DEFAULT_CACHE_TTL

    def _cfg_http_timeout(self) -> int:
        try:
            v = int(getattr(config, "OPTION_CONTRACTS_HTTP_TIMEOUT_SECONDS",
                            _DEFAULT_HTTP_TIMEOUT))
            return max(1, v)
        except Exception:
            return _DEFAULT_HTTP_TIMEOUT

    def _cfg_base_url(self) -> str:
        return str(getattr(config, "OPTION_CONTRACTS_UPSTOX_BASE_URL",
                           _DEFAULT_BASE_URL)).rstrip("/")

    # ------------------------------------------------------------------ #
    # Cache                                                               #
    # ------------------------------------------------------------------ #
    def _cache_key(self, underlying_key: str,
                   expiry_key: str) -> tuple[str, str]:
        return (underlying_key.strip(), expiry_key.strip().lower())

    def _cache_get(
        self, underlying_key: str, expiry_key: str,
    ) -> tuple[list[dict], int] | None:
        """Return ``(contracts, age_seconds)`` on a valid hit, else ``None``."""
        key = self._cache_key(underlying_key, expiry_key)
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            fetched_at, contracts = entry
            age = int(time.time() - fetched_at)
            ttl = self._cfg_cache_ttl()
            if ttl > 0 and age > ttl:
                self._cache.pop(key, None)
                return None
            return contracts, age

    def _cache_put(
        self, underlying_key: str, expiry_key: str, contracts: list[dict],
    ) -> None:
        key = self._cache_key(underlying_key, expiry_key)
        with self._lock:
            self._cache[key] = (time.time(), contracts)

    def clear_cache(self) -> dict:
        """Drop every cached entry. Returns a small status dict."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        logger.info("[option-contracts] cache cleared (%d entries)", count)
        return {"status": "ok", "cleared": count, "cleared_at": _now_iso()}

    # ------------------------------------------------------------------ #
    # Raw Upstox fetch                                                    #
    # ------------------------------------------------------------------ #
    def _http_fetch_raw(
        self,
        underlying_key: str,
        expiry_value: str | None,
    ) -> list[dict]:
        """Call Upstox and return the raw ``data`` list.

        ``expiry_value`` semantics:

            * ``None``        — do NOT pass ``expiry_date`` (fetch ALL).
            * ``"all"``       — do NOT pass ``expiry_date`` (fetch ALL).
            * otherwise       — pass ``expiry_date=<value>`` verbatim.
        """
        token = _resolve_access_token()
        url = f"{self._cfg_base_url()}/option/contract"
        params: dict[str, Any] = {"instrument_key": underlying_key}
        if expiry_value and expiry_value.lower() != _ALL_EXPIRY_SENTINEL:
            params["expiry_date"] = expiry_value

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        timeout = self._cfg_http_timeout()

        logger.info(
            "[option-contracts] GET %s params=%s (timeout=%ss)",
            url, params, timeout,
        )

        try:
            resp = requests.get(
                url, params=params, headers=headers, timeout=timeout,
            )
        except requests.RequestException as exc:
            raise OptionContractsServiceError(
                f"Upstox request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code != 200:
            snippet = ""
            try:
                snippet = resp.text[:500]
            except Exception:
                pass
            raise OptionContractsServiceError(
                f"Upstox returned HTTP {resp.status_code}: {snippet}"
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise OptionContractsServiceError(
                f"Upstox returned non-JSON body: {type(exc).__name__}: {exc}"
            ) from exc

        status = str(payload.get("status") or "").lower()
        if status != "success":
            raise OptionContractsServiceError(
                f"Upstox status={payload.get('status')!r} "
                f"errors={payload.get('errors')!r}"
            )

        data = payload.get("data")
        if not isinstance(data, list):
            raise OptionContractsServiceError(
                "Upstox response missing 'data' list"
            )

        logger.info(
            "[option-contracts] fetched %d contract(s) for %s (expiry=%s)",
            len(data), underlying_key, expiry_value or "ALL",
        )
        return data

    # ------------------------------------------------------------------ #
    # Public: raw (cached) list                                           #
    # ------------------------------------------------------------------ #
    def fetch_raw(
        self,
        underlying_key: str | None = None,
        expiry_date: str | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """Return the raw (unfiltered) contract list for an underlying.

        ``expiry_date`` accepts the same forms as :meth:`fetch`.
        Caching is per ``(underlying_key, expiry_key)`` tuple.
        """
        underlying = (underlying_key or self._cfg_default_underlying()).strip()
        if not underlying:
            raise OptionContractsServiceError("underlying_key is empty")

        normalized_expiry = _normalize_expiry_input(expiry_date)
        if normalized_expiry is None:
            normalized_expiry = self._cfg_default_expiry_keyword()

        cache_key = normalized_expiry or _ALL_EXPIRY_SENTINEL

        if not force_refresh:
            hit = self._cache_get(underlying, cache_key)
            if hit is not None:
                contracts, age = hit
                return {
                    "status": "ok",
                    "underlying_key": underlying,
                    "expiry_key": cache_key,
                    "count": len(contracts),
                    "contracts": contracts,
                    "cache": {
                        "hit": True,
                        "ttl_seconds": self._cfg_cache_ttl(),
                        "age_seconds": age,
                    },
                    "fetched_at": _now_iso(),
                }

        contracts = self._http_fetch_raw(underlying, normalized_expiry)
        self._cache_put(underlying, cache_key, contracts)

        return {
            "status": "ok",
            "underlying_key": underlying,
            "expiry_key": cache_key,
            "count": len(contracts),
            "contracts": contracts,
            "cache": {
                "hit": False,
                "ttl_seconds": self._cfg_cache_ttl(),
                "age_seconds": 0,
            },
            "fetched_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Filtering helpers                                                   #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _filter_contracts(
        raw: list[dict],
        strike_min: float | None,
        strike_max: float | None,
        instrument_type: str | None,
        concrete_expiry: str | None,
    ) -> list[dict]:
        want_type = (instrument_type or "").strip().upper() or None
        if want_type not in (None, "CE", "PE"):
            raise OptionContractsServiceError(
                f"instrument_type must be CE or PE (got {instrument_type!r})"
            )

        expiry_norm: str | None = None
        if concrete_expiry:
            s = str(concrete_expiry).strip()[:10]
            if s and s.lower() != _ALL_EXPIRY_SENTINEL \
                    and not _is_relative_expiry_keyword(s):
                expiry_norm = s

        out: list[dict] = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            strike = _safe_float(c.get("strike_price"))
            if strike is None:
                continue
            if strike_min is not None and strike < strike_min:
                continue
            if strike_max is not None and strike > strike_max:
                continue
            if want_type is not None:
                t = str(c.get("instrument_type") or "").strip().upper()
                if t != want_type:
                    continue
            if expiry_norm is not None:
                e = str(c.get("expiry") or "").strip()[:10]
                if e != expiry_norm:
                    continue
            out.append(c)
        return out

    @staticmethod
    def _collect_expiries(raw: list[dict]) -> list[str]:
        seen: set[str] = set()
        for c in raw:
            if not isinstance(c, dict):
                continue
            e = c.get("expiry")
            if not e:
                continue
            s = str(e).strip()[:10]
            if s:
                seen.add(s)
        return sorted(seen)

    @staticmethod
    def _nearest_expiry(raw: list[dict], today: date) -> str | None:
        expiries = OptionContractsService._collect_expiries(raw)
        for iso in expiries:
            try:
                d = date.fromisoformat(iso)
            except Exception:
                continue
            if d >= today:
                return iso
        return expiries[0] if expiries else None

    # ------------------------------------------------------------------ #
    # Public: filtered fetch                                              #
    # ------------------------------------------------------------------ #
    def fetch(
        self,
        underlying_key: str | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiry_date: str | None = None,
        instrument_type: str | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """Fetch and filter option contracts.

        All parameters are optional. Defaults:

            * ``underlying_key``   — ``config.OPTION_CONTRACTS_DEFAULT_UNDERLYING``
                                     (falls back to ``NSE_INDEX|Nifty 50``)
            * ``strike_min``       — ``config.OPTION_CONTRACTS_DEFAULT_STRIKE_MIN`` (21000)
            * ``strike_max``       — ``config.OPTION_CONTRACTS_DEFAULT_STRIKE_MAX`` (25000)
            * ``expiry_date``      — ``config.OPTION_CONTRACTS_DEFAULT_EXPIRY_KEYWORD``
                                     (falls back to ``current_week``)
            * ``instrument_type``  — ``None`` (both CE and PE)

        Returned ``contracts`` are raw Upstox dicts. ``nearest_expiry`` is
        computed from the fetched set as an aid; ``available_expiries``
        lists every expiry present in the fetched set.
        """
        underlying = (underlying_key or self._cfg_default_underlying()).strip()

        # Apply per-field default fallbacks only when the caller did
        # NOT supply a value. ``None`` means "caller omitted"; explicit
        # ``0`` / ``999999`` etc. must be honoured (some callers want
        # the whole chain unfiltered by strike).
        eff_strike_min = (
            float(strike_min) if strike_min is not None
            else self._cfg_default_strike_min()
        )
        eff_strike_max = (
            float(strike_max) if strike_max is not None
            else self._cfg_default_strike_max()
        )
        if eff_strike_min > eff_strike_max:
            raise OptionContractsServiceError(
                f"strike_min ({eff_strike_min}) must be <= strike_max "
                f"({eff_strike_max})"
            )

        normalized_expiry = _normalize_expiry_input(expiry_date)
        if normalized_expiry is None:
            normalized_expiry = self._cfg_default_expiry_keyword()

        raw = self.fetch_raw(
            underlying_key=underlying,
            expiry_date=normalized_expiry,
            force_refresh=force_refresh,
        )

        filtered = self._filter_contracts(
            raw["contracts"],
            strike_min=eff_strike_min,
            strike_max=eff_strike_max,
            instrument_type=instrument_type,
            concrete_expiry=normalized_expiry,
        )

        today = datetime.now(_market_tz()).date()
        all_expiries = self._collect_expiries(raw["contracts"])
        nearest = self._nearest_expiry(raw["contracts"], today)

        return {
            "status": "ok",
            "underlying_key": underlying,
            "expiry_keyword": (
                normalized_expiry
                if _is_relative_expiry_keyword(normalized_expiry) else None
            ),
            "expiry_date": (
                normalized_expiry
                if normalized_expiry
                and normalized_expiry.lower() != _ALL_EXPIRY_SENTINEL
                and not _is_relative_expiry_keyword(normalized_expiry)
                else None
            ),
            "strike_min": eff_strike_min,
            "strike_max": eff_strike_max,
            "instrument_type": (
                (instrument_type or "").strip().upper() or None
            ),
            "count": len(filtered),
            "contracts": filtered,
            "nearest_expiry": nearest,
            "available_expiries": all_expiries,
            "cache": raw["cache"],
            "fetched_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Public: single-contract lookup                                      #
    # ------------------------------------------------------------------ #
    def find_contract(
        self,
        strike: float,
        instrument_type: str,
        underlying_key: str | None = None,
        expiry_date: str | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """Return the contract matching strike + type (+ optional expiry).

        The strike filter is unbounded — the caller's default 21000-25000
        range would otherwise hide strikes outside it. When multiple
        expiries match the strike (because ``expiry_date`` is a relative
        keyword), the earliest expiry is returned.

        On no match returns ``{"status": "not_found", ...}`` with
        ``available_expiries`` + ``nearest_expiry`` so the caller can
        course-correct.
        """
        if strike is None:
            raise OptionContractsServiceError("strike is required")
        try:
            strike_f = float(strike)
        except Exception as exc:
            raise OptionContractsServiceError(
                f"strike must be numeric (got {strike!r})"
            ) from exc
        if strike_f <= 0:
            raise OptionContractsServiceError(
                f"strike must be positive (got {strike_f})"
            )

        itype = (instrument_type or "").strip().upper()
        if itype not in ("CE", "PE"):
            raise OptionContractsServiceError(
                f"instrument_type must be 'CE' or 'PE' "
                f"(got {instrument_type!r})"
            )

        result = self.fetch(
            underlying_key=underlying_key,
            strike_min=0.0,
            strike_max=1_000_000.0,
            expiry_date=expiry_date,
            instrument_type=itype,
            force_refresh=force_refresh,
        )

        candidates: list[dict] = []
        for c in result["contracts"]:
            s = _safe_float(c.get("strike_price"))
            if s is not None and abs(s - strike_f) < 1e-6:
                candidates.append(c)

        if candidates:
            # Prefer the earliest expiry — matches user expectation of
            # "the contract" for a strike/type when multiple expiries
            # are in play.
            candidates.sort(key=lambda c: str(c.get("expiry") or ""))
            best = candidates[0]
            return {
                "status": "ok",
                "contract": best,
                "underlying_key": result["underlying_key"],
                "cache": result["cache"],
                "fetched_at": result["fetched_at"],
            }

        return {
            "status": "not_found",
            "underlying_key": result["underlying_key"],
            "strike": strike_f,
            "instrument_type": itype,
            "expiry_keyword": result.get("expiry_keyword"),
            "expiry_date": result.get("expiry_date"),
            "nearest_expiry": result.get("nearest_expiry"),
            "available_expiries": result.get("available_expiries"),
            "cache": result["cache"],
            "fetched_at": result["fetched_at"],
        }

    # ------------------------------------------------------------------ #
    # Public: expiries listing                                            #
    # ------------------------------------------------------------------ #
    def list_expiries(
        self,
        underlying_key: str | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """Return the sorted list of available expiry dates.

        Fetches the full chain (no expiry filter). Large payload —
        cached. Returns ``nearest_expiry`` for convenience.
        """
        raw = self.fetch_raw(
            underlying_key=underlying_key,
            expiry_date=_ALL_EXPIRY_SENTINEL,
            force_refresh=force_refresh,
        )
        expiries = self._collect_expiries(raw["contracts"])
        today = datetime.now(_market_tz()).date()
        return {
            "status": "ok",
            "underlying_key": raw["underlying_key"],
            "expiries": expiries,
            "nearest_expiry": self._nearest_expiry(raw["contracts"], today),
            "count": len(expiries),
            "cache": raw["cache"],
            "fetched_at": _now_iso(),
        }


option_contracts_service = OptionContractsService()


# --------------------------------------------------------------------------- #
# Tiny CLI for manual testing                                                 #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json
    import sys

    try:
        if len(sys.argv) > 1 and sys.argv[1] == "expiries":
            print(json.dumps(
                option_contracts_service.list_expiries(),
                indent=2, default=str,
            ))
        else:
            print(json.dumps(
                option_contracts_service.fetch(),
                indent=2, default=str,
            ))
    except OptionContractsServiceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)