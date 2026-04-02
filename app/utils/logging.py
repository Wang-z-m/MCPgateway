from __future__ import annotations

import contextvars
import json
import logging
from typing import Any


request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

SENSITIVE_KEYS = {"authorization", "api_key", "x-api-key", "token", "password"}


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(message)s")


def set_request_id(request_id: str) -> None:
    request_id_var.set(request_id)


def get_request_id() -> str:
    return request_id_var.get()


def redact_sensitive(data: Any) -> Any:
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if key.lower() in SENSITIVE_KEYS:
                redacted[key] = "***"
            else:
                redacted[key] = redact_sensitive(value)
        return redacted
    if isinstance(data, list):
        return [redact_sensitive(item) for item in data]
    return data


def log_json(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, "request_id": get_request_id(), **redact_sensitive(fields)}
    logger.log(level, json.dumps(payload, ensure_ascii=False))
