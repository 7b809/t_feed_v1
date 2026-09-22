from datetime import date, datetime
from pathlib import Path
from typing import Any

from core import config
from utils.common import parse_timestamp
from utils.json_store import read_json, write_json_atomic


def slug(contract: dict) -> str:
    strike = float(contract["strike_price"])

    strike_text = (
        str(int(strike))
        if strike.is_integer()
        else str(strike)
    )

    option_type = str(
        contract["option_type"]
    ).upper().strip()

    return f"{strike_text}_{option_type}"


def state_path(contract: dict) -> Path:
    return (
        config.EMA_STATE_ROOT
        / f"{slug(contract)}.json"
    )


def ema_cross_folder(contract: dict) -> Path:
    return (
        config.EMA_CROSS_ROOT
        / slug(contract)
    )


def historical_crosses_path(
    contract: dict,
) -> Path:
    return (
        ema_cross_folder(contract)
        / "historical_crosses.json"
    )


def intraday_crosses_path(
    contract: dict,
) -> Path:
    return (
        ema_cross_folder(contract)
        / "intraday_crosses.json"
    )


def load_state(
    contract: dict,
    trading_date: date,
) -> dict | None:
    payload = read_json(
        state_path(contract),
        {},
    )

    if not isinstance(payload, dict):
        return None

    instrument = payload.get(
        "instrument",
        {},
    )

    if not isinstance(instrument, dict):
        return None

    if (
        payload.get("trading_date")
        != trading_date.isoformat()
    ):
        return None

    if (
        instrument.get("instrument_key")
        != contract.get("instrument_key")
    ):
        return None

    if (
        payload.get("ema_fast_period")
        != config.EMA_FAST_PERIOD
    ):
        return None

    if (
        payload.get("ema_slow_period")
        != config.EMA_SLOW_PERIOD
    ):
        return None

    state = payload.get("state")

    return (
        state
        if isinstance(state, dict)
        else None
    )


def save_state(
    contract: dict,
    trading_date: date,
    state: dict,
    warmup_complete: bool = True,
) -> None:
    payload = {
        "status": "ready",
        "trading_date": trading_date.isoformat(),
        "updated_at": datetime.now(
            config.MARKET_TIMEZONE
        ).isoformat(),
        "instrument": contract,
        "ema_fast_period": (
            config.EMA_FAST_PERIOD
        ),
        "ema_slow_period": (
            config.EMA_SLOW_PERIOD
        ),
        "historical_warmup_complete": (
            warmup_complete
        ),
        "state": state,
    }

    write_json_atomic(
        state_path(contract),
        payload,
    )


def _valid_cross_rows(
    rows: Any,
) -> list:
    if not isinstance(
        rows,
        (list, tuple, set),
    ):
        return []

    return [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and row.get("timestamp")
            and row.get("cross_type")
        )
    ]


def _cross_key(row: dict) -> str:
    timestamp = str(
        row.get("timestamp") or ""
    ).strip()

    cross_type = str(
        row.get("cross_type") or ""
    ).strip().lower()

    return f"{timestamp}|{cross_type}"


def deduplicate_crosses(
    rows: Any,
) -> list:
    unique: dict[str, dict] = {}

    for row in _valid_cross_rows(rows):
        unique[_cross_key(row)] = row

    return sorted(
        unique.values(),
        key=lambda row: str(
            row.get("timestamp") or ""
        ),
    )


def _instrument_matches(
    payload: Any,
    contract: dict,
) -> bool:
    if not isinstance(payload, dict):
        return False

    instrument = payload.get(
        "instrument",
        {},
    )

    if not isinstance(instrument, dict):
        return False

    return (
        instrument.get("instrument_key")
        == contract.get("instrument_key")
    )


def _cross_date_range(
    rows: list[dict],
) -> dict[str, str | None]:
    dates: list[str] = []

    for row in rows:
        timestamp = parse_timestamp(
            row.get("timestamp")
        )

        if timestamp is not None:
            dates.append(
                timestamp.date().isoformat()
            )

    if not dates:
        return {
            "from_date": None,
            "to_date": None,
        }

    return {
        "from_date": min(dates),
        "to_date": max(dates),
    }


def _build_cross_payload(
    *,
    contract: dict,
    cross_scope: str,
    rows: Any,
    trading_date: date | None = None,
) -> dict:
    normalized_rows = deduplicate_crosses(
        rows
    )

    bullish_count = sum(
        row.get("is_bullish_cross") is True
        or str(
            row.get("cross_type") or ""
        ).lower() == "bullish"
        for row in normalized_rows
    )

    bearish_count = sum(
        row.get("is_bearish_cross") is True
        or str(
            row.get("cross_type") or ""
        ).lower() == "bearish"
        for row in normalized_rows
    )

    date_range = _cross_date_range(
        normalized_rows
    )

    return {
        "status": "success",
        "cross_scope": cross_scope,
        "generated_at": datetime.now(
            config.MARKET_TIMEZONE
        ).isoformat(),
        "trading_date": (
            trading_date.isoformat()
            if trading_date is not None
            else None
        ),
        "from_date": date_range["from_date"],
        "to_date": date_range["to_date"],
        "instrument": contract,
        "ema_fast_period": (
            config.EMA_FAST_PERIOD
        ),
        "ema_slow_period": (
            config.EMA_SLOW_PERIOD
        ),
        "total_cross_count": len(
            normalized_rows
        ),
        "bullish_cross_count": (
            bullish_count
        ),
        "bearish_cross_count": (
            bearish_count
        ),
        "latest_cross": (
            normalized_rows[-1]
            if normalized_rows
            else None
        ),
        "crossovers": normalized_rows,
    }


def _existing_crosses(
    path: Path,
    contract: dict,
    trading_date: date | None = None,
) -> list:
    payload = read_json(
        path,
        {},
    )

    if not _instrument_matches(
        payload,
        contract,
    ):
        return []

    if trading_date is not None:
        if (
            payload.get("trading_date")
            != trading_date.isoformat()
        ):
            return []

    return deduplicate_crosses(
        payload.get(
            "crossovers",
            [],
        )
    )


def save_historical_crosses(
    contract: dict,
    rows: list[dict],
) -> None:
    if not getattr(
        config,
        "SAVE_HISTORICAL_CROSSES",
        True,
    ):
        return

    path = historical_crosses_path(
        contract
    )

    rebuild = getattr(
        config,
        "REBUILD_CROSSES_ON_REFRESH",
        True,
    )

    if rebuild:
        final_rows = deduplicate_crosses(
            rows
        )
    else:
        existing_rows = _existing_crosses(
            path,
            contract,
        )

        final_rows = deduplicate_crosses(
            [
                *existing_rows,
                *rows,
            ]
        )

    payload = _build_cross_payload(
        contract=contract,
        cross_scope="historical",
        rows=final_rows,
    )

    write_json_atomic(
        path,
        payload,
    )


def save_intraday_crosses(
    contract: dict,
    trading_date: date,
    rows: list[dict],
) -> None:
    if not getattr(
        config,
        "SAVE_INTRADAY_CROSSES",
        True,
    ):
        return

    path = intraday_crosses_path(
        contract
    )

    rebuild = getattr(
        config,
        "REBUILD_CROSSES_ON_REFRESH",
        True,
    )

    if rebuild:
        final_rows = deduplicate_crosses(
            rows
        )
    else:
        existing_rows = _existing_crosses(
            path,
            contract,
            trading_date,
        )

        final_rows = deduplicate_crosses(
            [
                *existing_rows,
                *rows,
            ]
        )

    payload = _build_cross_payload(
        contract=contract,
        cross_scope="intraday",
        trading_date=trading_date,
        rows=final_rows,
    )

    write_json_atomic(
        path,
        payload,
    )


def append_intraday_crossover(
    contract: dict,
    trading_date: date,
    candle: dict,
) -> bool:
    if not getattr(
        config,
        "SAVE_INTRADAY_CROSSES",
        True,
    ):
        return False

    if not isinstance(candle, dict):
        return False

    if not candle.get("cross_type"):
        return False

    candle_timestamp = parse_timestamp(
        candle.get("timestamp")
    )

    if candle_timestamp is None:
        return False

    if (
        candle_timestamp.date()
        != trading_date
    ):
        return False

    path = intraday_crosses_path(
        contract
    )

    existing_rows = _existing_crosses(
        path,
        contract,
        trading_date,
    )

    existing_keys = {
        _cross_key(row)
        for row in existing_rows
    }

    candle_key = _cross_key(
        candle
    )

    if candle_key in existing_keys:
        return False

    final_rows = deduplicate_crosses(
        [
            *existing_rows,
            candle,
        ]
    )

    payload = _build_cross_payload(
        contract=contract,
        cross_scope="intraday",
        trading_date=trading_date,
        rows=final_rows,
    )

    write_json_atomic(
        path,
        payload,
    )

    return True


def crossover_files_exist(
    contract: dict,
    trading_date: date | None = None,
) -> bool:
    historical_required = getattr(
        config,
        "SAVE_HISTORICAL_CROSSES",
        True,
    )

    intraday_required = getattr(
        config,
        "SAVE_INTRADAY_CROSSES",
        True,
    )

    if historical_required:
        historical_path = (
            historical_crosses_path(
                contract
            )
        )

        if not historical_path.exists():
            return False

        historical_payload = read_json(
            historical_path,
            {},
        )

        if not _instrument_matches(
            historical_payload,
            contract,
        ):
            return False

    if intraday_required:
        intraday_path = (
            intraday_crosses_path(
                contract
            )
        )

        if not intraday_path.exists():
            return False

        intraday_payload = read_json(
            intraday_path,
            {},
        )

        if not _instrument_matches(
            intraday_payload,
            contract,
        ):
            return False

        if (
            trading_date is not None
            and intraday_payload.get(
                "trading_date"
            )
            != trading_date.isoformat()
        ):
            return False

    return True


def load_historical_crosses(
    contract: dict,
) -> dict:
    payload = read_json(
        historical_crosses_path(
            contract
        ),
        {},
    )

    if not _instrument_matches(
        payload,
        contract,
    ):
        return {}

    return payload


def load_intraday_crosses(
    contract: dict,
    trading_date: date | None = None,
) -> dict:
    payload = read_json(
        intraday_crosses_path(
            contract
        ),
        {},
    )

    if not _instrument_matches(
        payload,
        contract,
    ):
        return {}

    if (
        trading_date is not None
        and payload.get("trading_date")
        != trading_date.isoformat()
    ):
        return {}

    return payload


def append_completed_candle(
    contract: dict,
    trading_date: date,
    candle: dict,
) -> None:
    if not config.SAVE_ALL_COMPLETED_CANDLES:
        return

    if not isinstance(candle, dict):
        return

    timestamp = candle.get("timestamp")

    if not timestamp:
        return

    path = (
        config.CANDLE_ROOT
        / trading_date.isoformat()
        / f"{slug(contract)}.json"
    )

    payload = read_json(
        path,
        {},
    )

    rows = (
        payload.get("candles", [])
        if isinstance(payload, dict)
        else []
    )

    valid_rows = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and row.get("timestamp")
            != timestamp
        )
    ]

    valid_rows.append(candle)

    valid_rows.sort(
        key=lambda row: str(
            row.get("timestamp") or ""
        )
    )

    write_json_atomic(
        path,
        {
            "status": "success",
            "generated_at": datetime.now(
                config.MARKET_TIMEZONE
            ).isoformat(),
            "instrument": contract,
            "trading_date": (
                trading_date.isoformat()
            ),
            "total_candle_count": len(
                valid_rows
            ),
            "candles": valid_rows,
        },
    )