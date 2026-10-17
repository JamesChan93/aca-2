"""Redact before JSON serialisation, including third-party standard logging records."""

import logging
import re
import sys
import threading
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import quote, quote_plus

import structlog
from pydantic import SecretStr

_SENSITIVE_KEY = re.compile(
    r"password|secret|token|authorization|cookie|api.?key|api.?sign|webhook", re.I
)
_URL_AUTH = re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", re.I)
_DISCORD = re.compile(r"https://[^\s/]*discord[^\s/]*/api/webhooks/[^\s\"']+", re.I)
_ASSIGNMENT = re.compile(r"(?i)\b(password|token|secret|api[_-]?key)=([^\s&]+)")
_known: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str) -> None:
    with _lock:
        _known.update({value, quote(value, safe=""), quote_plus(value)})


def redact(value: object) -> object:
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__}
    if isinstance(value, str):
        with _lock:
            secrets = sorted(_known, key=len, reverse=True)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
        value = _DISCORD.sub("[REDACTED_WEBHOOK]", value)
        return _ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    return value


def _redact_event(
    logger: object,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> dict[str, Any]:
    # Exception strings/tracebacks may include SQL, driver arguments or unvalidated inputs.
    for key in ("exc_info", "stack", "stack_info", "exception"):
        event_dict.pop(key, None)
    result = redact(dict(event_dict))
    if not isinstance(result, dict):
        raise TypeError("redacted log event must be an object")
    return result


def configure_logging(level: str = "INFO") -> None:
    shared: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_event,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _redact_event,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
