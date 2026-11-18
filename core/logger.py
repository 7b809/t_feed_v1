"""Central console, rotating-file and plain-text logging.

All output is passed through ``_ScrubbingFormatter``, which redacts
Cloudflare cookies, ``set-cookie`` / ``authorization`` header values,
``Bearer`` tokens, and JSON ``access_token`` fields from the fully
formatted message **including tracebacks**. This is defense-in-depth:
``services.order_service._exception_text`` already scrubs the audit
ledger / MongoDB / Telegram path, but ``logger.exception()`` writes the
raw traceback to disk via the standard logging machinery and would
otherwise leak the Cloudflare ``__cf_bm`` cookie seen in the
2026-11-18 14:55:12 production log.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

from core.config import LOG_LEVEL, LOGS_DIR, LOGS_TEXT_DIR

_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)
_FORMAT = (
    "%(asctime)s [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] "
    "[%(funcName)s] %(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Patterns are applied in order to the fully formatted record (message +
# args + traceback). They intentionally target only values that can carry
# secrets; status codes, reqid, CF-RAY, and Upstox error bodies are left
# intact so support tickets remain diagnosable.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cloudflare bot-management cookie value. The value itself is
    # base64-ish with dots and underscores; the trailing Expires/SameSite
    # attributes are handled by the generic set-cookie rule below.
    (re.compile(r"__cf_bm=[^;'\"\s]+"), "__cf_bm=<redacted>"),
    # Any set-cookie header value, quoted or unquoted.
    (
        re.compile(
            r"(set-cookie['\"]?\s*:\s*['\"]?)[^'\"\)\r\n]+",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    # Any authorization header value, quoted or unquoted.
    (
        re.compile(
            r"(authorization['\"]?\s*:\s*['\"]?)[^'\"\)\r\n]+",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    # Bearer <token> anywhere it appears.
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    # JSON access_token field.
    (
        re.compile(r'("access_token"\s*:\s*")[^"]+(")'),
        r"\1<redacted>\2",
    ),
)


class _ScrubbingFormatter(logging.Formatter):
    """Formatter that redacts sensitive values from the final string.

    Redaction runs on the *formatted* output so it covers the log message,
    formatted arguments, and the full traceback produced by
    ``logger.exception``. Because the formatter is applied at the handler
    level, every destination (console, ``logs/*.log``, ``logs_text/*.txt``)
    receives the same scrubbed text.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pattern, replacement in _REDACTIONS:
            text = pattern.sub(replacement, text)
        return text


_FORMATTER = _ScrubbingFormatter(_FORMAT, datefmt=_DATE_FMT)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger with a console and two file destinations."""
    logger_name = Path(name).stem
    logger = logging.getLogger(logger_name)
    logger.setLevel(_LEVEL)
    logger.propagate = False
    if logger.handlers:
        return logger

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    console = logging.StreamHandler()
    console.setLevel(_LEVEL)
    console.setFormatter(_FORMATTER)
    logger.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"{logger_name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setLevel(_LEVEL)
    rotating.setFormatter(_FORMATTER)
    logger.addHandler(rotating)

    text_file = logging.FileHandler(
        LOGS_TEXT_DIR / f"{logger_name}.txt", mode="a", encoding="utf-8"
    )
    text_file.setLevel(_LEVEL)
    text_file.setFormatter(_FORMATTER)
    logger.addHandler(text_file)
    return logger