"""One-time handoff of option-chain history into the isolation archive."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from core.logger import get_logger

logger = get_logger(__file__)
_LOCK = threading.RLock()


def _find_contract_folder(instrument_key: str, trade_date: str) -> Path | None:
    root = Path(os.getenv("OPTION_CHAIN_DATA_DIR", "data/option_chain"))
    if not root.is_dir():
        return None
    date_key = str(trade_date).replace("-", "")
    for meta_path in root.glob(f"*/{date_key}/*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(meta.get("instrument_key") or "") == str(instrument_key):
                return meta_path.parent
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable option-chain metadata: %s", meta_path)
    return None


def seed_option_chain_history(
    instrument_key: str,
    trade_date: str,
    role: str,
    indicator: str,
    destination: str | Path,
) -> dict:
    """Merge option-chain rows into destination once; retry if source is not ready."""
    source_name = {
        "live_feed": "live_feed.jsonl",
        "supertrend": "supertrend_feed.jsonl",
    }.get(str(indicator))
    if not source_name:
        return {"state": "unsupported_indicator", "rows": 0}

    destination = Path(destination)
    isolation_root = Path(os.getenv("ISOLATED_DATA_DIR", "data/isolated"))
    manifest_path = isolation_root / ".option_chain_history_seed.json"
    seed_id = "|".join((str(trade_date), str(role), str(indicator), str(instrument_key)))

    with _LOCK:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("History seed manifest unreadable; rebuilding: %s", manifest_path)
            manifest = {}
        if seed_id in manifest:
            return {"state": "already_seeded", "rows": manifest[seed_id].get("rows", 0)}

        contract_folder = _find_contract_folder(instrument_key, trade_date)
        if contract_folder is None:
            return {"state": "source_missing", "rows": 0}
        source = contract_folder / source_name
        if not source.is_file():
            return {"state": "source_missing", "rows": 0}

        destination.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = source.read_bytes()
        source_is_complete = not source_bytes or source_bytes.endswith(b"\\n")
        combined = {}
        for candidate in (source, destination):
            if not candidate.is_file():
                continue
            with candidate.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Skipping incomplete JSONL row while seeding %s", candidate)
                        continue
                    combined[raw] = None
        if not combined:
            return {"state": "source_empty", "rows": 0}

        rows = sorted(combined, key=lambda raw: json.loads(raw).get("received_at_ist") or "")
        temporary = destination.with_suffix(destination.suffix + ".seed.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                for raw in rows:
                    handle.write(raw + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

        if not source_is_complete:
            logger.info("Option-chain history source still has an incomplete trailing row; will retry key=%s", instrument_key)
            return {"state": "source_partial", "rows": len(rows)}

        manifest[seed_id] = {
            "instrument_key": str(instrument_key),
            "trade_date_ist": str(trade_date),
            "role": str(role),
            "indicator": str(indicator),
            "source": str(source),
            "destination": str(destination),
            "rows": len(rows),
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        manifest_tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(manifest_tmp, manifest_path)
        logger.info("Option-chain history seeded indicator=%s role=%s key=%s rows=%d", indicator, role, instrument_key, len(rows))
        return {"state": "seeded", "rows": len(rows), "source": str(source), "destination": str(destination)}
