from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.auth import ApiKeyAuth
from app.core.errors import GatewayError
from app.core.rate_limit import SlidingWindowRateLimiter


def _make_mock_request(api_key: str | None = None) -> Request:
    headers_dict = {}
    if api_key is not None:
        headers_dict["x-api-key"] = api_key
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers_dict.items()],
    }
    return Request(scope)


def test_auth_describe_reports_enabled_keys() -> None:
    auth = ApiKeyAuth("gateway-key", "admin-key")

    summary = auth.describe()

    assert summary["gateway_key_enabled"] is True
    assert summary["admin_key_enabled"] is True


def test_auth_describe_reports_disabled_keys() -> None:
    auth = ApiKeyAuth("", "")

    summary = auth.describe()

    assert summary["gateway_key_enabled"] is False
    assert summary["admin_key_enabled"] is False


def test_auth_allows_valid_gateway_key() -> None:
    auth = ApiKeyAuth("correct-key", "admin-key")
    request = _make_mock_request("correct-key")

    auth.require_gateway_key(request)


def test_auth_rejects_invalid_gateway_key() -> None:
    auth = ApiKeyAuth("correct-key", "admin-key")
    request = _make_mock_request("wrong-key")

    with pytest.raises(GatewayError) as exc_info:
        auth.require_gateway_key(request)

    assert exc_info.value.category == "UNAUTHORIZED"


def test_auth_rejects_missing_gateway_key() -> None:
    auth = ApiKeyAuth("correct-key", "admin-key")
    request = _make_mock_request(None)

    with pytest.raises(GatewayError) as exc_info:
        auth.require_gateway_key(request)

    assert exc_info.value.category == "UNAUTHORIZED"


def test_auth_skips_validation_when_key_is_empty() -> None:
    auth = ApiKeyAuth("", "")
    request = _make_mock_request(None)

    auth.require_gateway_key(request)
    auth.require_admin_key(request)


def test_auth_admin_key_rejects_gateway_key() -> None:
    auth = ApiKeyAuth("gw-key", "admin-key")
    request = _make_mock_request("gw-key")

    with pytest.raises(GatewayError) as exc_info:
        auth.require_admin_key(request)

    assert exc_info.value.category == "UNAUTHORIZED"


def test_auth_admin_key_falls_back_to_gateway_key() -> None:
    auth = ApiKeyAuth("shared-key", "")
    request = _make_mock_request("shared-key")

    auth.require_admin_key(request)


def test_rate_limiter_returns_remaining_budget_and_retry_after() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)

    first = limiter.enforce("client-a")
    second = limiter.enforce("client-a")

    assert first.remaining == 1
    assert second.remaining == 0

    with pytest.raises(GatewayError) as exc_info:
        limiter.enforce("client-a")

    assert exc_info.value.category == "RATE_LIMITED"
    assert exc_info.value.data["remaining"] == 0
    assert exc_info.value.data["retry_after_seconds"] >= 1


def test_rate_limiter_isolates_different_clients() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60)

    limiter.enforce("client-a")
    status_b = limiter.enforce("client-b")

    assert status_b.remaining == 0

    with pytest.raises(GatewayError):
        limiter.enforce("client-a")

    with pytest.raises(GatewayError):
        limiter.enforce("client-b")


def test_rate_limiter_describe_returns_config() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=30)

    desc = limiter.describe()

    assert desc["max_requests"] == 100
    assert desc["window_seconds"] == 30
