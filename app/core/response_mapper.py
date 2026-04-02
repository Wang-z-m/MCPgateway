from __future__ import annotations

import json
import re
from typing import Any

from app.core.error_mapper import gateway_error
from app.core.rest_connector import RestResponse
from app.models.tool_config import ResponseMapping


_PATH_PATTERN = re.compile(r"([^[.\]]+)|\[(\d+)\]")


def _parse_path(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for match in _PATH_PATTERN.finditer(path):
        key, index = match.groups()
        parts.append(key if key is not None else int(index))
    return parts


def _get_value(data: Any, path: str | None) -> Any:
    if not path:
        return data
    current = data
    try:
        for part in _parse_path(path):
            if isinstance(part, int):
                current = current[part]
            else:
                current = current[part]
        return current
    except (KeyError, IndexError, TypeError) as exc:
        raise gateway_error(
            "DOWNSTREAM_ERROR",
            "Response mapping could not extract the configured result path.",
            data={"result_path": path},
        ) from exc


def _filter_fields(data: Any, field_whitelist: list[str]) -> Any:
    if not field_whitelist or not isinstance(data, dict):
        return data
    return {field: data.get(field) for field in field_whitelist}


def build_tool_result(payload: Any, response: RestResponse, mapping: ResponseMapping) -> dict[str, Any]:
    selected = _filter_fields(_get_value(payload, mapping.result_path), mapping.field_whitelist)
    if mapping.text_template and isinstance(selected, dict):
        try:
            text = mapping.text_template.format(**selected)
        except KeyError as exc:
            missing_key = str(exc).strip("'")
            raise gateway_error(
                "INTERNAL_ERROR",
                "Response text template references a missing field.",
                data={"missing_field": missing_key, "text_template": mapping.text_template},
            ) from exc
    else:
        text = json.dumps(selected, ensure_ascii=False, indent=2)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": selected,
    }
    if mapping.include_http_meta:
        result["meta"] = {
            "status_code": response.status_code,
            "elapsed_ms": response.elapsed_ms,
            "attempts_made": response.attempts_made,
        }
    return result
