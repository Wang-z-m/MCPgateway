from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.core.schema_generator import OpenApiSchemaGenerator
from app.models.tool_config import ToolConfig

SUPPORTED_CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}


class ConfigLoader:
    def __init__(self, config_dir: Path, openapi_dir: Path | None = None) -> None:
        self.config_dir = config_dir
        self.openapi_dir = openapi_dir
        self.schema_generator = OpenApiSchemaGenerator()

    def load_tools(self) -> tuple[list[ToolConfig], str]:
        configs: list[ToolConfig] = []
        file_paths = self._list_supported_files(self.config_dir, required=True)
        for file_path in file_paths:
            payload = self._read_file(file_path)
            tool = ToolConfig.model_validate(payload)
            tool.source_file = str(file_path)
            configs.append(tool)
        if not configs:
            raise ValueError(f"No tool configurations were loaded from '{self.config_dir}'.")
        config_hash = self._build_hash(configs)
        return configs, config_hash

    def preview_openapi_tools(self) -> list[ToolConfig]:
        if self.openapi_dir is None or not self.openapi_dir.exists():
            return []
        generated: list[ToolConfig] = []
        for file_path in self._list_supported_files(self.openapi_dir, required=False):
            spec = self._read_file(file_path)
            generated.extend(self.schema_generator.generate_tools(spec))
        return generated

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        with file_path.open("r", encoding="utf-8") as handle:
            if file_path.suffix.lower() == ".json":
                payload = json.load(handle)
            else:
                payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict):
            raise ValueError(
                f"Configuration file '{file_path.name}' must contain a JSON/YAML object at the top level."
            )
        return payload

    def _list_supported_files(self, directory: Path, *, required: bool) -> list[Path]:
        if not directory.exists():
            if required:
                raise FileNotFoundError(f"Configuration directory '{directory}' does not exist.")
            return []
        if not directory.is_dir():
            raise NotADirectoryError(f"Configuration path '{directory}' is not a directory.")
        file_paths = [
            file_path
            for file_path in sorted(directory.glob("*"))
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_CONFIG_SUFFIXES
        ]
        if required and not file_paths:
            raise ValueError(f"No supported config files found in '{directory}'.")
        return file_paths

    def _build_hash(self, configs: list[ToolConfig]) -> str:
        serializable = [
            config.model_dump(mode="json", exclude={"source_file"}) for config in configs
        ]
        raw = json.dumps(serializable, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.md5(raw).hexdigest()
