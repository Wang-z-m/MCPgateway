from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.core.error_mapper import gateway_error
from app.utils.logging import log_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RateLimitStatus:
    max_requests: int
    window_seconds: int
    remaining: int
    retry_after_seconds: int


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def enforce(self, key: str) -> RateLimitStatus:
        now = time.time()
        window_start = now - self.window_seconds
        bucket = self._events[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            retry_after_seconds = max(1, int(bucket[0] + self.window_seconds - now))
            log_json(
                logger,
                logging.WARNING,
                "rate_limited",
                limit=self.max_requests,
                window_seconds=self.window_seconds,
                retry_after_seconds=retry_after_seconds,
            )
            raise gateway_error(
                "RATE_LIMITED",
                "Rate limit exceeded.",
                data={
                    "window_seconds": self.window_seconds,
                    "max_requests": self.max_requests,
                    "remaining": 0,
                    "retry_after_seconds": retry_after_seconds,
                },
            )
        bucket.append(now)
        remaining = max(0, self.max_requests - len(bucket))
        retry_after_seconds = max(0, int(bucket[0] + self.window_seconds - now)) if bucket else 0
        return RateLimitStatus(
            max_requests=self.max_requests,
            window_seconds=self.window_seconds,
            remaining=remaining,
            retry_after_seconds=retry_after_seconds,
        )

    def describe(self) -> dict[str, int]:
        return {
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
        }
