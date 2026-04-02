from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(slots=True)
class PreparedRestRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = None
    timeout_seconds: float = 10.0
    retry_count: int = 1
    idempotent: bool = False


@dataclass(slots=True)
class RestResponse:
    status_code: int
    headers: dict[str, str]
    body: Any
    text: str
    elapsed_ms: int
    attempts_made: int = 1


class RestConnector:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, request: PreparedRestRequest) -> RestResponse:
        attempts = 1 + max(0, request.retry_count) if request.idempotent else 1
        last_exception: Exception | None = None

        for attempt in range(attempts):
            try:
                start = time.perf_counter()
                response = await self._client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    params=request.params,
                    json=request.json_body,
                    timeout=request.timeout_seconds,
                )
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                try:
                    body: Any = response.json()
                except ValueError:
                    body = response.text
                should_retry_status = (
                    request.idempotent
                    and response.status_code >= 500
                    and attempt < attempts - 1
                )
                if should_retry_status:
                    continue
                return RestResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=body,
                    text=response.text,
                    elapsed_ms=elapsed_ms,
                    attempts_made=attempt + 1,
                )
            except httpx.TimeoutException as exc:
                last_exception = exc
                if attempt == attempts - 1:
                    raise
            except httpx.RequestError as exc:
                last_exception = exc
                if attempt == attempts - 1:
                    raise

        if last_exception:
            raise last_exception
        raise RuntimeError("REST request failed without an explicit exception.")
