from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging import log_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Session:
    session_id: str
    created_at: float
    last_active_at: float
    initialized: bool = False
    client_info: dict[str, Any] | None = None
    protocol_version: str = "2025-03-26"


class SessionManager:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        client_info: dict[str, Any] | None = None,
        protocol_version: str = "2025-03-26",
    ) -> Session:
        async with self._lock:
            session_id = secrets.token_hex(32)
            now = time.time()
            session = Session(
                session_id=session_id,
                created_at=now,
                last_active_at=now,
                client_info=client_info,
                protocol_version=protocol_version,
            )
            self._sessions[session_id] = session
            log_json(logger, logging.INFO, "session_created", session_id=session_id)
            return session

    async def get_session(self, session_id: str) -> Session | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if time.time() - session.last_active_at > self.ttl_seconds:
                del self._sessions[session_id]
                log_json(
                    logger, logging.INFO, "session_expired", session_id=session_id
                )
                return None
            session.last_active_at = time.time()
            return session

    async def mark_initialized(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.initialized = True
            session.last_active_at = time.time()
            log_json(
                logger, logging.INFO, "session_initialized", session_id=session_id
            )
            return True

    async def terminate_session(self, session_id: str) -> bool:
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                log_json(
                    logger,
                    logging.INFO,
                    "session_terminated",
                    session_id=session_id,
                )
                return True
            return False

    async def cleanup_expired(self) -> int:
        async with self._lock:
            now = time.time()
            expired = [
                sid
                for sid, s in self._sessions.items()
                if now - s.last_active_at > self.ttl_seconds
            ]
            for sid in expired:
                del self._sessions[sid]
            if expired:
                log_json(
                    logger,
                    logging.INFO,
                    "sessions_cleanup",
                    removed_count=len(expired),
                    remaining_count=len(self._sessions),
                )
            return len(expired)

    def active_count(self) -> int:
        return len(self._sessions)

    def describe(self) -> dict[str, Any]:
        return {
            "active_sessions": len(self._sessions),
            "ttl_seconds": self.ttl_seconds,
        }
