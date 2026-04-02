from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from app.models.tool_config import (
    ErrorMapping,
    HttpTarget,
    RequestMapping,
    ResponseMapping,
    ToolConfig,
    ToolMeta,
)


class OpenApiSchemaGenerator:
    SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete"}

    def load_spec(self, file_path: Path) -> dict[str, Any]:
        with file_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def generate_tools(
        self,
        spec: dict[str, Any],
        *,
        fallback_base_url: str = "http://127.0.0.1:9001",
    ) -> list[ToolConfig]:
        servers = spec.get("servers") or []
        base_url = servers[0]["url"] if servers else fallback_base_url
        generated: list[ToolConfig] = []

        for path, operations in spec.get("paths", {}).items():
            for method, operation in operations.items():
                if method not in self.SUPPORTED_METHODS:
                    continue
                operation_id = operation.get("operationId") or self._build_operation_id(method, path)
                title = operation.get("summary") or operation_id.replace("_", " ").title()
                description = operation.get("description") or title
                input_schema, request_mapping = self._build_input_schema(operation, path)
                generated.append(
                    ToolConfig(
                        tool_meta=ToolMeta(
                            name=operation_id,
                            title=title,
                            description=description,
                            tags=operation.get("tags", []),
                        ),
                        input_schema=input_schema,
                        http_target=HttpTarget(
                            method=method.upper(),
                            base_url=base_url,
                            path=path,
                        ),
                        request_mapping=request_mapping,
                        response_mapping=ResponseMapping(result_path="data"),
                        error_mapping=ErrorMapping(),
                    )
                )
        return generated

    def _build_operation_id(self, method: str, path: str) -> str:
        normalized = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        normalized = normalized or "root"
        return f"{method}_{normalized}"

    def _build_input_schema(self, operation: dict[str, Any], path: str) -> tuple[dict[str, Any], RequestMapping]:
        schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        mapping = RequestMapping()
        for parameter in operation.get("parameters", []):
            name = parameter["name"]
            location = parameter["in"]
            property_schema = deepcopy(parameter.get("schema", {"type": "string"}))
            property_schema["description"] = parameter.get("description", "")
            schema["properties"][name] = property_schema
            if parameter.get("required"):
                schema["required"].append(name)
            if location == "path":
                mapping.path_map[name] = name
            elif location == "query":
                mapping.query_map[name] = name
            elif location == "header":
                mapping.header_map[name] = name

        request_body = operation.get("requestBody", {})
        content = request_body.get("content", {}).get("application/json", {})
        body_schema = deepcopy(content.get("schema"))
        if isinstance(body_schema, dict) and body_schema.get("type") == "object":
            for field_name, field_schema in body_schema.get("properties", {}).items():
                schema["properties"][field_name] = field_schema
                mapping.body_map[field_name] = field_name
            schema["required"].extend(body_schema.get("required", []))

        schema["required"] = sorted(set(schema["required"]))
        if not schema["properties"]:
            schema["additionalProperties"] = False
        return schema, mapping
