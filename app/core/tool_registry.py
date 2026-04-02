from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.config_loader import ConfigLoader
from app.core.error_mapper import gateway_error
from app.db.repositories import ConfigAuditRepository
from app.models.tool_config import ToolConfig


@dataclass(slots=True)
class ToolSnapshot:
    version: str
    config_hash: str
    loaded_at: str
    tools: dict[str, ToolConfig]


class ToolRegistry:
    def __init__(
        self,
        loader: ConfigLoader,
        audit_repository: ConfigAuditRepository,
    ) -> None:
        self.loader = loader
        self.audit_repository = audit_repository
        self.active_snapshot: ToolSnapshot | None = None
        self.previous_snapshot: ToolSnapshot | None = None
        self._reload_lock = asyncio.Lock()

    async def load_initial_snapshot(self) -> None:
        async with self._reload_lock:
            snapshot = self._build_snapshot()
            self.active_snapshot = snapshot
            await self.audit_repository.record_event(
                version=snapshot.version,
                config_hash=snapshot.config_hash,
                status="loaded",
            )

    async def reload(self) -> dict[str, Any]:
        async with self._reload_lock:
            old_snapshot = self.active_snapshot
            old_version = old_snapshot.version if old_snapshot else None
            try:
                candidate = self._build_snapshot()
                change_summary = self._build_change_summary(old_snapshot, candidate)
                self.previous_snapshot = self.active_snapshot
                self.active_snapshot = candidate
                await self.audit_repository.record_event(
                    version=candidate.version,
                    config_hash=candidate.config_hash,
                    status="reloaded",
                    rollback_from=old_version,
                )
                return {
                    "status": "ok",
                    "version": candidate.version,
                    "previous_version": old_version,
                    "tool_count": len(candidate.tools),
                    "loaded_at": candidate.loaded_at,
                    "config_hash": candidate.config_hash,
                    "changes": change_summary,
                }
            except Exception as exc:
                active = self.active_snapshot
                await self.audit_repository.record_event(
                    version=active.version if active else "uninitialized",
                    config_hash=active.config_hash if active else "none",
                    status="reload_failed",
                    error_message=str(exc),
                    rollback_from=old_version,
                )
                raise

    async def rollback(self) -> dict[str, Any]:
        async with self._reload_lock:
            if self.previous_snapshot is None:
                raise ValueError("No previous snapshot available for rollback.")
            current = self.active_snapshot
            current_version = current.version if current else None
            target = self.previous_snapshot
            change_summary = self._build_change_summary(current, target)
            self.active_snapshot = target
            self.previous_snapshot = current
            await self.audit_repository.record_event(
                version=target.version,
                config_hash=target.config_hash,
                status="rolled_back",
                rollback_from=current_version,
            )
            return {
                "status": "ok",
                "version": target.version,
                "rolled_back_from": current_version,
                "tool_count": len(target.tools),
                "loaded_at": target.loaded_at,
                "config_hash": target.config_hash,
                "changes": change_summary,
            }

    def list_tools(self) -> list[ToolConfig]:
        if not self.active_snapshot:
            return []
        return list(self.active_snapshot.tools.values())

    def describe_tools(self) -> list[dict[str, Any]]:
        return [self._serialize_tool(tool) for tool in self.list_tools()]

    def preview_tools(self) -> list[dict[str, Any]]:
        return [self._serialize_tool(tool) for tool in self.loader.preview_openapi_tools()]

    def get_tool(self, name: str) -> ToolConfig:
        if not self.active_snapshot or name not in self.active_snapshot.tools:
            available_tools = (
                sorted(self.active_snapshot.tools) if self.active_snapshot else []
            )
            raise gateway_error(
                "TOOL_NOT_FOUND",
                f"Tool '{name}' does not exist.",
                data={"tool_name": name, "available_tools": available_tools},
            )
        return self.active_snapshot.tools[name]

    def status(self) -> dict[str, str | int | None]:
        if not self.active_snapshot:
            return {"status": "empty", "tool_count": 0, "loaded_at": None, "previous_version": None}
        return {
            "status": "ready",
            "version": self.active_snapshot.version,
            "config_hash": self.active_snapshot.config_hash,
            "tool_count": len(self.active_snapshot.tools),
            "loaded_at": self.active_snapshot.loaded_at,
            "previous_version": self.previous_snapshot.version if self.previous_snapshot else None,
        }

    def management_summary(self) -> dict[str, Any]:
        active_tools = self.list_tools()
        return {
            "snapshot": self.status(),
            "active_tool_names": sorted(tool.tool_meta.name for tool in active_tools),
            "active_tool_count": len(active_tools),
            "preview_tool_count": len(self.loader.preview_openapi_tools()),
        }

    def _build_snapshot(self) -> ToolSnapshot:
        tools, config_hash = self.loader.load_tools()
        if not tools:
            raise ValueError("Tool snapshot cannot be empty.")
        seen_names: set[str] = set()
        for tool in tools:
            if tool.tool_meta.name in seen_names:
                raise ValueError(f"Duplicate tool name detected: {tool.tool_meta.name}")
            seen_names.add(tool.tool_meta.name)
        now = datetime.now(timezone.utc)
        loaded_at = now.isoformat()
        version = now.strftime("%Y%m%d%H%M%S%f")
        return ToolSnapshot(
            version=version,
            config_hash=config_hash,
            loaded_at=loaded_at,
            tools={tool.tool_meta.name: tool for tool in tools},
        )

    def _build_change_summary(
        self,
        previous: ToolSnapshot | None,
        candidate: ToolSnapshot,
    ) -> dict[str, Any]:
        previous_tools = previous.tools if previous else {}
        candidate_tools = candidate.tools

        previous_names = set(previous_tools)
        candidate_names = set(candidate_tools)

        added = sorted(candidate_names - previous_names)
        removed = sorted(previous_names - candidate_names)
        updated = sorted(
            name
            for name in (candidate_names & previous_names)
            if self._fingerprint_tool(candidate_tools[name])
            != self._fingerprint_tool(previous_tools[name])
        )

        return {
            "added": added,
            "removed": removed,
            "updated": updated,
            "unchanged_count": len(candidate_names & previous_names) - len(updated),
        }

    def _serialize_tool(self, tool: ToolConfig) -> dict[str, Any]:
        required_fields = tool.input_schema.get("required", [])
        properties = tool.input_schema.get("properties", {})
        return {
            "name": tool.tool_meta.name,
            "title": tool.tool_meta.title,
            "description": tool.tool_meta.description,
            "version": tool.tool_meta.version,
            "tags": tool.tool_meta.tags,
            "method": tool.http_target.method,
            "path": tool.http_target.path,
            "source_file": tool.source_file,
            "required_fields": required_fields,
            "property_count": len(properties) if isinstance(properties, dict) else 0,
        }

    def _fingerprint_tool(self, tool: ToolConfig) -> str:
        payload = tool.model_dump(mode="json", exclude={"source_file"})
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)
