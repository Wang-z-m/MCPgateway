from __future__ import annotations

from pathlib import Path

import aiosqlite


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    rollback_from TEXT
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_type TEXT,
                    downstream_status INTEGER,
                    latency_ms INTEGER NOT NULL,
                    attempts_made INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(
                conn,
                table_name="access_log",
                column_name="attempts_made",
                column_definition="INTEGER NOT NULL DEFAULT 1",
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_config_audit_published_at
                ON config_audit(published_at DESC)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_access_log_created_at
                ON access_log(created_at DESC)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_access_log_tool_name
                ON access_log(tool_name)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_access_log_success
                ON access_log(success)
                """
            )
            await conn.commit()

    async def connect(self) -> aiosqlite.Connection:
        return await aiosqlite.connect(self.db_path)

    async def _ensure_column(
        self,
        conn: aiosqlite.Connection,
        *,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        cursor = await conn.execute(f"PRAGMA table_info({table_name})")
        rows = await cursor.fetchall()
        existing_columns = {row[1] for row in rows}
        if column_name not in existing_columns:
            await conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
            )
