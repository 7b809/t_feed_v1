# app/services/telegram_service.py
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import Settings

logger = logging.getLogger(__name__)


class TelegramService:
    """Best-effort Telegram notifier backed by the sync ``requests`` library.

    Design goals
    ------------
    * **Never raises.** All public methods return ``bool``. Failures are
      logged as warnings and swallowed so the caller's flow continues.
    * **Non-blocking for async callers.** The sync send is wrapped in
      ``asyncio.to_thread``; each worker thread gets its own
      ``requests.Session`` (thread-safety + connection reuse).
    * **Retries transient failures** — connection errors, 429, 5xx — with
      short exponential backoff (capped; does not honour Retry-After).
    * **Truncates** oversized messages below Telegram's 4096-char limit.
    * **No-ops** when Telegram is disabled or not configured.

    The failure path is the "notify and continue" path: every error is
    reported through ``logger.warning`` (and ``stderr`` for scripts
    without logging configured), then the method returns ``False``. The
    application is never taken down because a notification failed.
    """

    _EMOJI = {
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "STARTUP": "🚀",
    }
    _MAX_TEXT_LEN = 4000  # Telegram limit is 4096; leave headroom

    def __init__(self, config: Settings) -> None:
        self._config = config
        # Per-thread sessions: requests.Session is not thread-safe, and
        # asyncio.to_thread may run calls on different worker threads.
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    @property
    def _enabled(self) -> bool:
        return (
            self._config.telegram_enabled
            and bool(self._config.telegram_bot_token)
            and bool(self._config.telegram_chat_id)
        )

    @property
    def _timeout(self) -> float:
        return float(getattr(self._config, "telegram_timeout", 5.0) or 5.0)

    # ------------------------------------------------------------------
    # Per-thread session with retry policy
    # ------------------------------------------------------------------
    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            raise_on_status=False,
            # Don't sit on a 120 s Retry-After for a fire-and-forget ping.
            respect_retry_after_header=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"Content-Type": "application/json"})
        self._local.session = session
        return session

    # ------------------------------------------------------------------
    # Failure reporting — the "notify and continue" branch
    # ------------------------------------------------------------------
    def _report_failure(
        self,
        reason: str,
        text: str,
        exc: Optional[BaseException] = None,
        body: Optional[str] = None,
    ) -> None:
        snippet = str(text)[:200].replace("\n", " ")
        msg = (
            f"telegram delivery failed ({reason}); continuing. "
            f"text={snippet!r}"
        )
        if body:
            msg += f" body={body[:200]!r}"

        logger.warning(msg, exc_info=exc)

        # Startup scripts / CLI tools may not have logging configured;
        # make the failure visible on stderr too so it lands in nohup logs.
        if exc is not None:
            print(f"WARNING: {msg}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Core sync sender — never raises
    # ------------------------------------------------------------------
    def _send_sync(self, text: str) -> bool:
        if not self._enabled:
            return False

        text = self._truncate(str(text))
        url = (
            "https://api.telegram.org/bot"
            f"{self._config.telegram_bot_token}/sendMessage"
        )
        payload = {
            "chat_id": self._config.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            resp = self._get_session().post(
                url, json=payload, timeout=self._timeout
            )
        except requests.exceptions.Timeout:
            self._report_failure("timeout", text)
            return False
        except requests.exceptions.ConnectionError as exc:
            self._report_failure("connection error", text, exc=exc)
            return False
        except requests.exceptions.RequestException as exc:
            self._report_failure("request error", text, exc=exc)
            return False
        except Exception as exc:  # defensive — never crash the app
            self._report_failure("unexpected error", text, exc=exc)
            return False

        if resp.status_code >= 400:
            self._report_failure(
                f"http {resp.status_code}",
                text,
                body=resp.text,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Async wrapper — preserves the existing async API
    # ------------------------------------------------------------------
    async def _send(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            return await asyncio.to_thread(self._send_sync, text)
        except Exception as exc:
            self._report_failure("thread dispatch error", text, exc=exc)
            return False

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= TelegramService._MAX_TEXT_LEN:
            return text
        return text[: TelegramService._MAX_TEXT_LEN - 20] + "\n…[truncated]"

    # ------------------------------------------------------------------
    # Public API — safe to await, always returns bool
    # ------------------------------------------------------------------
    async def send_message(
        self,
        title: str,
        message: str,
        level: str = "INFO",
    ) -> bool:
        emoji = self._EMOJI.get(str(level).upper(), "ℹ️")
        return await self._send(f"{emoji} {title}\n\n{message}")

    async def notify_startup(self, app_name: str) -> bool:
        return await self._send(f"✅ {app_name} started")

    async def notify_shutdown(self, app_name: str) -> bool:
        return await self._send(f"🛑 {app_name} stopped")

    async def notify_isolation(
        self, isolation_id: str, instrument_key: str
    ) -> bool:
        return await self._send(
            f"📥 Isolation received: {isolation_id} | leg={instrument_key}"
        )

    async def notify_ws_state(
        self,
        instrument_key: str,
        leg_role: str,
        state: str,
        detail: str = "",
    ) -> bool:
        tail = f" — {detail}" if detail else ""
        return await self._send(
            f"🔌 WS {leg_role} {instrument_key} → {state}{tail}"
        )

    # ------------------------------------------------------------------
    # Sync convenience for non-async callers (updater, CLI tools)
    # ------------------------------------------------------------------
    def send_message_sync(
        self,
        title: str,
        message: str,
        level: str = "INFO",
    ) -> bool:
        emoji = self._EMOJI.get(str(level).upper(), "ℹ️")
        return self._send_sync(f"{emoji} {title}\n\n{message}")


    async def send_document(self, file_path: str, caption: str = "") -> bool:
        """Upload one document to Telegram without blocking the event loop.

        Uses a dedicated multipart request: the shared JSON session header
        used by sendMessage must not be applied to multipart/form-data.
        This method is best-effort and never raises to the caller.
        """
        if not self._enabled:
            logger.warning("telegram document upload skipped: Telegram is disabled or not configured")
            return False

        path = str(file_path)
        url = (
            "https://api.telegram.org/bot"
            f"{self._config.telegram_bot_token}/sendDocument"
        )

        def _upload() -> bool:
            try:
                with open(path, "rb") as document:
                    response = requests.post(
                        url,
                        data={
                            "chat_id": self._config.telegram_chat_id,
                            "caption": self._truncate(caption) if caption else "",
                        },
                        files={"document": (path.split("/")[-1], document, "application/octet-stream")},
                        timeout=(10, 180),
                    )
                if response.status_code >= 400:
                    self._report_failure(
                        f"document http {response.status_code}",
                        caption or path,
                        body=response.text,
                    )
                    return False
                try:
                    body = response.json()
                    if body.get("ok") is False:
                        self._report_failure("document API rejected upload", caption or path,
                                             body=str(body))
                        return False
                except ValueError:
                    self._report_failure("document invalid response", caption or path,
                                         body=response.text)
                    return False
                return True
            except requests.exceptions.RequestException as exc:
                self._report_failure("document request error", caption or path, exc=exc)
                return False
            except Exception as exc:
                self._report_failure("document unexpected error", caption or path, exc=exc)
                return False

        try:
            return await asyncio.to_thread(_upload)
        except Exception as exc:
            self._report_failure("document thread dispatch error", caption or path, exc=exc)
            return False

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._local.session = None