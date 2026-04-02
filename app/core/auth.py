from __future__ import annotations

import hmac
import logging

from fastapi import Request

from app.core.error_mapper import gateway_error
from app.utils.logging import log_json

logger = logging.getLogger(__name__)


class ApiKeyAuth:
    def __init__(self, api_key: str, admin_api_key: str) -> None:
        self.api_key = api_key
        self.admin_api_key = admin_api_key

    def require_gateway_key(self, request: Request) -> None:
        expected = self.api_key.strip()
        if not expected:
            return
        provided = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(provided, expected):
            log_json(logger, logging.WARNING, "gateway_auth_failed", path=request.url.path)
            raise gateway_error("UNAUTHORIZED", "Invalid gateway API key.")

    def require_admin_key(self, request: Request) -> None:
        expected = self.admin_api_key.strip() or self.api_key.strip()
        if not expected:
            return
        provided = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(provided, expected):
            log_json(logger, logging.WARNING, "admin_auth_failed", path=request.url.path)
            raise gateway_error("UNAUTHORIZED", "Invalid admin API key.")

    def describe(self) -> dict[str, bool]:
        return {
            "gateway_key_enabled": bool(self.api_key.strip()),
            "admin_key_enabled": bool((self.admin_api_key.strip() or self.api_key.strip())),
        }
