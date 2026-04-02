from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.database import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfigAuditRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_event(
        self,
        *,
        version: str,
        config_hash: str,
        status: str,
        error_message: str | None = None,
        rollback_from: str | None = None,
    ) -> None:
        conn = await self.database.connect()
        try:
            await conn.execute(
                """
                INSERT INTO config_audit(version, config_hash, published_at, status, error_message, rollback_from)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (version, config_hash, utc_now(), status, error_message, rollback_from),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def latest_events(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = await self.database.connect()
        try:
            cursor = await conn.execute(
                """
                SELECT version, config_hash, published_at, status, error_message, rollback_from
                FROM config_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
        return [
            {
                "version": row[0],
                "config_hash": row[1],
                "published_at": row[2],
                "status": row[3],
                "error_message": row[4],
                "rollback_from": row[5],
            }
            for row in rows
        ]

    async def build_summary(self) -> dict[str, Any]:
        conn = await self.database.connect()
        try:
            summary_cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) AS total_events,
                    COALESCE(SUM(CASE WHEN status = 'reload_failed' THEN 1 ELSE 0 END), 0) AS failed_events,
                    COALESCE(MAX(published_at), '') AS last_published_at
                FROM config_audit
                """
            )
            summary = await summary_cursor.fetchone()
        finally:
            await conn.close()

        return {
            "total_events": summary[0] or 0,
            "failed_events": summary[1] or 0,
            "last_published_at": summary[2] or None,
        }


class AccessLogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_call(
        self,
        *,
        request_id: str,
        tool_name: str,
        success: bool,
        error_type: str | None,
        downstream_status: int | None,
        latency_ms: int,
        attempts_made: int = 1,
    ) -> None:
        conn = await self.database.connect()
        try:
            await conn.execute(
                """
                INSERT INTO access_log(request_id, tool_name, success, error_type, downstream_status, latency_ms, attempts_made, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    tool_name,
                    int(success),
                    error_type,
                    downstream_status,
                    latency_ms,
                    attempts_made,
                    utc_now(),
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    async def build_metrics_snapshot(self) -> dict[str, Any]:
        conn = await self.database.connect()
        try:
            summary_cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) AS total_calls,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                    COALESCE(SUM(success), 0) AS success_calls,
                    COALESCE(MAX(created_at), '') AS last_call_at
                FROM access_log
                """
            )
            summary = await summary_cursor.fetchone()

            tool_cursor = await conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*),
                    COALESCE(AVG(latency_ms), 0),
                    COALESCE(SUM(success), 0)
                FROM access_log
                GROUP BY tool_name
                ORDER BY COUNT(*) DESC
                """
            )
            tool_rows = await tool_cursor.fetchall()

            error_cursor = await conn.execute(
                """
                SELECT error_type, COUNT(*)
                FROM access_log
                WHERE success = 0
                GROUP BY error_type
                ORDER BY COUNT(*) DESC
                """
            )
            error_rows = await error_cursor.fetchall()
        finally:
            await conn.close()

        total_calls = summary[0] or 0
        success_calls = summary[2] or 0
        failure_calls = total_calls - success_calls
        success_rate = (success_calls / total_calls) if total_calls else 0.0
        return {
            "total_calls": total_calls,
            "success_calls": success_calls,
            "failure_calls": failure_calls,
            "avg_latency_ms": round(float(summary[1] or 0), 2),
            "success_rate": round(success_rate, 4),
            "last_call_at": summary[3] or None,
            "by_tool": [
                {
                    "tool_name": row[0],
                    "call_count": row[1],
                    "avg_latency_ms": round(float(row[2] or 0), 2),
                    "success_rate": round((row[3] / row[1]) if row[1] else 0.0, 4),
                }
                for row in tool_rows
            ],
            "error_distribution": [
                {"error_type": row[0] or "UNKNOWN", "count": row[1]} for row in error_rows
            ],
        }

    async def latest_calls(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = await self.database.connect()
        try:
            cursor = await conn.execute(
                """
                SELECT
                    request_id,
                    tool_name,
                    success,
                    error_type,
                    downstream_status,
                    latency_ms,
                    attempts_made,
                    created_at
                FROM access_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()

        return [
            {
                "request_id": row[0],
                "tool_name": row[1],
                "success": bool(row[2]),
                "error_type": row[3],
                "downstream_status": row[4],
                "latency_ms": row[5],
                "attempts_made": row[6],
                "created_at": row[7],
            }
            for row in rows
        ]
