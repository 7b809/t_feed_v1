from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from app.config import Settings

logger = logging.getLogger(__name__)


class MegaDownloadError(RuntimeError):
    pass


def validate_mega_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {"mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"}:
        raise MegaDownloadError("file_link must be a supported HTTPS MEGA public link")
    if not parsed.fragment and "/file/" not in parsed.path and "!/" not in url:
        raise MegaDownloadError("MEGA link does not look like a public file link")


class MegaDownloadService:
    """Adapter for the installed MEGA SDK; SDK-specific behavior stays isolated here."""

    def __init__(self, config: Settings):
        self.config = config

    def download(self, url: str, expected_name: str, expected_size: int | None = None) -> Path:
        validate_mega_url(url)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for attempt in range(max(1, self.config.refresh_retries)):
            temp_dir = Path(tempfile.mkdtemp(prefix="mega-download-", dir=self.config.download_dir))
            try:
                self._sdk_download(url, temp_dir)
                candidates = [p for p in temp_dir.rglob("*") if p.is_file()]
                if len(candidates) != 1:
                    raise MegaDownloadError("MEGA SDK did not produce exactly one archive file")
                downloaded = candidates[0]
                if downloaded.stat().st_size <= 0:
                    raise MegaDownloadError("Downloaded archive is empty")
                if downloaded.stat().st_size > self.config.archive_limit_bytes:
                    raise MegaDownloadError("Downloaded archive exceeds MAX_ARCHIVE_SIZE_MB")
                if expected_size is not None and int(expected_size) != downloaded.stat().st_size:
                    raise MegaDownloadError("Downloaded size does not match archive_size_bytes")

                safe_name = Path(expected_name).name
                if safe_name in {"", ".", ".."} or safe_name != expected_name or "/" in expected_name or "\\" in expected_name:
                    safe_name = self.config.mega_archive_filename
                final_path = self.config.download_dir / safe_name
                staging_path = self.config.download_dir / f".{safe_name}.new"
                shutil.copy2(downloaded, staging_path)
                staging_path.replace(final_path)
                return final_path
            except Exception as exc:
                last_error = exc
                logger.warning("MEGA download attempt %s failed (%s)", attempt + 1, type(exc).__name__)
                if attempt + 1 < max(1, self.config.refresh_retries):
                    time.sleep(self.config.retry_backoff_seconds * (2 ** attempt))
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        if isinstance(last_error, MegaDownloadError):
            raise last_error
        raise MegaDownloadError("MEGA download failed; verify the public link and installed SDK") from last_error

    @staticmethod
    def _sdk_download(url: str, destination: Path) -> None:
        """
        SDK adapter. The package API differs between similarly named MEGA clients.
        This implementation targets the documented mega-sdk `Mega` interface.
        """
        try:
            from mega_sdk import Mega
        except ImportError as exc:
            raise MegaDownloadError(
                "Could not import Mega from mega_sdk. Verify the installed mega-sdk package/API."
            ) from exc
        try:
            mega = Mega()
            mega.login()  # anonymous login
            # Some builds expose download_url(url, dest_path); others expose
            # download_url(url, dest_path, dest_filename). Fail clearly rather
            # than silently switching to a different package's API.
            if hasattr(mega, "download_url"):
                try:
                    mega.download_url(url, str(destination))
                except TypeError:
                    mega.download_url(url, str(destination), None)
            elif hasattr(mega, "download_public_url"):
                mega.download_public_url(url, str(destination))
            else:
                raise MegaDownloadError(
                    "Installed mega-sdk has no supported public-link download method. "
                    "Inspect the installed package and adapt _sdk_download."
                )
        except MegaDownloadError:
            raise
        except Exception as exc:
            raise MegaDownloadError("MEGA anonymous download failed") from exc
