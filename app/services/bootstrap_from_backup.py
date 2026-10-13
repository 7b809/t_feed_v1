"""Bootstrap candle data from the MongoDB/MEGA backup.

Runs at application startup, before jobs 1-4. Priority:

1. **MongoDB blob** — the ``backup_historical_candles`` document may
   contain the archive bytes directly (``data`` field, ``bson.Binary``).
   If present and non-empty, extract it into ``settings.data_dir``.

2. **MEGA file link** — the same document also carries a ``file_link``
   to the archive on MEGA. It is used only when the document's
   ``updated_at`` (or ``updated_at_utc``) is from **today** in the
   market timezone. Otherwise the backup is considered stale and
   skipped.

3. **Fall through** — if neither path yields data, the caller (lifespan)
   runs the standard historical candle job, which fetches from Upstox.

Design notes
------------
* Never raises. Any failure is logged and reported in the summary.
* Extraction is skipped when the target file already exists, unless
  ``BOOTSTRAP_OVERWRITE=true``. This makes the bootstrap safe to run
  on every start.
* The archive may contain ``_manifest.txt`` and ``_metadata.json`` at
  the root — those are deliberately ignored.
* Both flat archives (``nifty/…``) and wrapped archives (``data/nifty/…``)
  are supported.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

# Archive-level files that are metadata, not candle data.
_METADATA_FILENAMES = {"_manifest.txt", "_metadata.json"}

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _market_tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(settings.market_timezone)
    except Exception:
        return timezone.utc

def _parse_updated_at(doc: dict) -> datetime | None:
    """Return the doc's update timestamp as an aware UTC datetime."""
    # Prefer the native datetime field if present.
    value = doc.get("updated_at_utc")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # Fall back to the IST string produced by candle-backup.
    value = doc.get("updated_at")
    if isinstance(value, str):
        try:
            tz = _market_tz()
            naive = datetime.strptime(
                value.replace(" IST", "").strip(),
                "%Y-%m-%d %H:%M:%S",
            )
            return naive.replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            return None
    return None

def _is_today_in_market_tz(ts: datetime | None) -> bool:
    """True when ``ts`` (aware UTC) falls on today's date in MARKET_TIMEZONE."""
    if ts is None:
        return False
    tz = _market_tz()
    return ts.astimezone(tz).date() == datetime.now(tz).date()

# ---------------------------------------------------------------------------
# Mongo helpers
# ---------------------------------------------------------------------------

def _client() -> MongoClient | None:
    if not settings.bootstrap_enabled:
        return None
    if not settings.bootstrap_mongo_url:
        logger.warning(
            "Bootstrap: BOOTSTRAP_ENABLED=true but "
            "BOOTSTRAP_MONGO_URL is empty; skipping"
        )
        return None
    try:
        return MongoClient(
            settings.bootstrap_mongo_url,
            serverSelectionTimeoutMS=settings.bootstrap_mongo_timeout_ms,
            connectTimeoutMS=settings.bootstrap_mongo_timeout_ms,
        )
    except Exception:
        logger.exception("Bootstrap: unable to create MongoClient")
        return None

def _read_blob_doc(client: MongoClient) -> dict | None:
    try:
        coll = client[settings.bootstrap_mongo_db][
            settings.bootstrap_mongo_blob_collection
        ]
        return coll.find_one({"_id": settings.bootstrap_mongo_blob_doc_id})
    except PyMongoError:
        logger.exception("Bootstrap: blob doc read failed")
        return None

# ---------------------------------------------------------------------------
# Zip extraction
# ---------------------------------------------------------------------------

def _detect_zip_prefix(zf: zipfile.ZipFile) -> str:
    """Return a top-level prefix to strip ('data/' or '').

    Older candle-backup archives wrapped the tree in a ``data/`` folder.
    Newer ones place the contents at the archive root. Both are handled.
    """
    names = {n for n in zf.namelist() if not n.endswith("/")}
    names -= _METADATA_FILENAMES
    if not names:
        return ""
    tops = {n.split("/", 1)[0] for n in names}
    if tops == {"data"}:
        return "data/"
    return ""

def _extract_zip_bytes(
    zip_bytes: bytes,
    dest_dir: Path,
    overwrite: bool,
) -> dict[str, int]:
    """Extract candle data from a zip into ``dest_dir``.

    Skips archive metadata files. When ``overwrite`` is False, existing
    files are left untouched. Rejects unsafe paths.

    Returns a dict with exactly four keys: ``total``, ``written``,
    ``skipped``, ``failed``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    result = {"total": 0, "written": 0, "skipped": 0, "failed": 0}

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            prefix = _detect_zip_prefix(zf)
            if prefix:
                logger.info(
                    "Bootstrap: stripping '%s' prefix from archive", prefix
                )

            dest_resolved = dest_dir.resolve()

            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                base = Path(name).name
                if base in _METADATA_FILENAMES:
                    continue

                result["total"] += 1

                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                if not rel:
                    continue

                target = (dest_dir / rel).resolve()

                # Reject traversal / absolute paths.
                try:
                    target.relative_to(dest_resolved)
                except ValueError:
                    logger.warning(
                        "Bootstrap: refusing unsafe path %r", name
                    )
                    result["failed"] += 1
                    continue

                if target.exists() and not overwrite:
                    result["skipped"] += 1
                    continue

                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    result["written"] += 1
                except Exception:
                    logger.exception(
                        "Bootstrap: failed to write %s", target
                    )
                    result["failed"] += 1
    except zipfile.BadZipFile:
        logger.exception("Bootstrap: bad zip archive")
        result["failed"] += 1

    return result

# ---------------------------------------------------------------------------
# MEGA download
# ---------------------------------------------------------------------------

def _download_direct(file_link: str) -> bytes | None:
    """Attempt a direct HTTP GET.

    Works for direct-download URLs. For standard ``mega.nz/#!`` links
    this returns an HTML landing page, which we detect and reject.
    """
    import requests

    try:
        resp = requests.get(
            file_link,
            timeout=settings.request_timeout_seconds,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Bootstrap: direct download failed")
        return None

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype or resp.content[:2] != b"PK":
        logger.info(
            "Bootstrap: link did not return a zip "
            "(Content-Type=%s, first bytes=%r)",
            ctype,
            resp.content[:2],
        )
        return None

    return resp.content

def _download_via_mega_local(file_link: str) -> bytes | None:
    """Fall back to the vendored ``mega_local`` client.

    Requires ``mega_local`` to be importable (either vendored at the
    project root or installed as a package).
    """
    try:
        from mega_local import Mega  # type: ignore
    except ImportError:
        logger.info(
            "Bootstrap: mega_local not available; cannot decrypt "
            "mega.nz/#! link"
        )
        return None

    import tempfile

    try:
        mega = Mega()
        with tempfile.TemporaryDirectory(prefix="bootstrap_mega_") as tmp:
            path = mega.download_url(file_link, dest_path=tmp)
            if path is None:
                return None
            return Path(path).read_bytes()
    except Exception:
        logger.exception("Bootstrap: mega_local download failed")
        return None

def _download_mega(file_link: str) -> bytes | None:
    """Download the archive from a MEGA link.

    Tries a direct HTTP GET first (cheap, works for direct URLs), then
    falls back to the vendored ``mega_local`` client for encrypted
    ``#!`` links.
    """
    logger.info("Bootstrap: downloading from MEGA: %s", file_link)
    data = _download_direct(file_link)
    if data is not None:
        logger.info("Bootstrap: direct download OK (%d bytes)", len(data))
        return data

    logger.info("Bootstrap: falling back to mega_local")
    data = _download_via_mega_local(file_link)
    if data is not None:
        logger.info("Bootstrap: mega_local download OK (%d bytes)", len(data))
    return data

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def bootstrap_from_backup() -> dict[str, Any]:
    """Try to seed ``data/`` from the MongoDB/MEGA backup.

    Never raises. Returns a summary dict with a stable shape:

        {
          "attempted": True,
          "source": "mongo-blob" | "mega" | "none",
          "reason": "...",          # populated when source == "none"
          "total": N,               # files present in the archive
          "written": N,             # files written to disk
          "skipped": N,             # files already present, not overwritten
          "failed": N,              # files that could not be written
        }
    """
    summary: dict[str, Any] = {
        "attempted": True,
        "source": "none",
        "reason": None,
        "total": 0,
        "written": 0,
        "skipped": 0,
        "failed": 0,
    }

    if not settings.bootstrap_enabled:
        summary["attempted"] = False
        summary["reason"] = "disabled"
        logger.info("Bootstrap: disabled via BOOTSTRAP_ENABLED")
        return summary

    client = _client()
    if client is None:
        summary["reason"] = "mongo-unavailable"
        return summary

    try:
        doc = _read_blob_doc(client)
        if doc is None:
            summary["reason"] = "blob-doc-missing"
            logger.info(
                "Bootstrap: no blob document at %s/%s/%s",
                settings.bootstrap_mongo_db,
                settings.bootstrap_mongo_blob_collection,
                settings.bootstrap_mongo_blob_doc_id,
            )
            return summary

        blob = doc.get("data")

        # ---- Path 1: inline blob in MongoDB ------------------------
        if blob:
            try:
                size = len(blob)
            except Exception:
                size = -1
            logger.info(
                "Bootstrap: inline blob found in MongoDB (%s bytes)", size
            )
            result = _extract_zip_bytes(
                bytes(blob),
                settings.data_dir,
                overwrite=settings.bootstrap_overwrite,
            )
            if result["written"] == 0 and result["failed"] > 0:
                logger.warning(
                    "Bootstrap: mongo blob yielded no files; "
                    "trying the MEGA link instead"
                )
            else:
                summary.update(result)
                summary["source"] = "mongo-blob"
                logger.info(
                    "Bootstrap: extracted from MongoDB blob: %s", result
                )
                return summary

        # ---- Path 2: MEGA link (only if from today) ----------------
        file_link = doc.get("file_link")
        status = doc.get("status")
        logger.info(
            "Bootstrap: no usable inline blob (status=%s); "
            "considering MEGA link",
            status,
        )

        if not file_link:
            summary["reason"] = "no-file-link"
            logger.info("Bootstrap: no MEGA file link in blob doc")
            return summary

        updated_at = _parse_updated_at(doc)
        if not _is_today_in_market_tz(updated_at):
            summary["reason"] = "mega-not-today"
            logger.info(
                "Bootstrap: MEGA backup is not from today "
                "(updated_at=%s, market_tz=%s)",
                updated_at,
                settings.market_timezone,
            )
            return summary

        zip_bytes = _download_mega(file_link)
        if zip_bytes is None:
            summary["reason"] = "mega-download-failed"
            logger.warning("Bootstrap: MEGA download returned no data")
            return summary

        result = _extract_zip_bytes(
            zip_bytes,
            settings.data_dir,
            overwrite=settings.bootstrap_overwrite,
        )
        summary.update(result)
        summary["source"] = "mega"
        logger.info("Bootstrap: extracted from MEGA: %s", result)
        return summary

    except Exception:
        logger.exception("Bootstrap: unexpected failure")
        summary["reason"] = "exception"
        return summary
    finally:
        try:
            client.close()
        except Exception:
            pass