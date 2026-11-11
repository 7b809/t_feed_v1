# app/logging_config.py
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Dict

from .config import Settings


class SensitiveDataRedactionFilter(logging.Filter):
    """Redact Telegram-style bot tokens (and chat ids) from log records."""

    TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
    TELEGRAM_CHAT_ID_RE = re.compile(r"(?i)\bchat[_\s]?id\b\s*[:=]\s*-?\d{4,}")
    REDACTED = "<REDACTED_TELEGRAM_TOKEN>"
    REDACTED_CHAT = "<REDACTED_TELEGRAM_CHAT_ID>"

    def _redact(self, value: str) -> str:
        value = self.TELEGRAM_TOKEN_RE.sub(self.REDACTED, value)
        value = self.TELEGRAM_CHAT_ID_RE.sub(self.REDACTED_CHAT, value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (self._redact(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        self._redact(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            pass
        return True


class PerModuleFileHandler(logging.Handler):
    """Writes each log record to logs/<module>.log based on record.name."""

    def __init__(self, logs_dir: str, formatter: logging.Formatter):
        super().__init__()
        self._logs_dir = logs_dir
        self._formatter = formatter
        self._handlers: Dict[str, logging.Handler] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            module = record.name.rsplit(".", 1)[-1] if record.name else "root"
            handler = self._handlers.get(module)
            if handler is None:
                path = os.path.join(self._logs_dir, f"{module}.log")
                handler = RotatingFileHandler(
                    path,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(self._formatter)
                self._handlers[module] = handler
            handler.handle(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for handler in self._handlers.values():
            try:
                handler.close()
            except Exception:
                pass
        self._handlers.clear()
        super().close()


def setup_logging(config: Settings) -> None:
    os.makedirs(config.logs_dir, exist_ok=True)

    level = getattr(logging, config.log_level.upper(), logging.INFO)

    # %(filename)s:%(lineno)d → e.g. "sr_ws_client_service.py:205"
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    redactor = SensitiveDataRedactionFilter()

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    combined_path = os.path.join(config.logs_dir, f"{config.app_name}.log")
    combined = RotatingFileHandler(
        combined_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    combined.setFormatter(fmt)
    combined.addFilter(redactor)
    root.addHandler(combined)

    per_module = PerModuleFileHandler(config.logs_dir, fmt)
    per_module.addFilter(redactor)
    root.addHandler(per_module)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)