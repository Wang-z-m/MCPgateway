from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from jsonschema import ValidationError, validate

from app.core.error_mapper import (
    from_http_error,
    from_request_error,
    from_timeout,
    from_unknown_exception,
    from_validation_error,
    gateway_error,
)
from app.core.errors import GatewayError
from app.core.response_mapper import build_tool_result
from app.core.rest_connector import PreparedRestRequest, RestConnector
from app.db.repositories import AccessLogRepository
from app.models.tool_config import ToolConfig
from app.utils.logging import get_request_id, log_json

logger = logging.getLogger(__name__)
_PATH_VARIABLE_PATTERN = re.compile(r"\{([^{}]+)\}")


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


class AdaptationEngine:
    def __init__(
        self,
        connector: RestConnector,
        access_log_repository: AccessLogRepository,
    ) -> None:
        self.connector = connector
        self.access_log_repository = access_log_repository

    async def execute_tool(self, tool: ToolConfig, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            validate(instance=arguments, schema=tool.input_schema)
            request = self._build_request(tool, arguments)
            response = await self.connector.send(request)
            if response.status_code >= 400:
                raise from_http_error(
                    status_code=response.status_code,
                    response_text=response.text,
                    elapsed_ms=response.elapsed_ms,
                    error_mapping=tool.error_mapping,
                )
            result = build_tool_result(response.body, response, tool.response_mapping)
            await self.access_log_repository.record_call(
                request_id=get_request_id(),
                tool_name=tool.tool_meta.name,
                success=True,
                error_type=None,
                downstream_status=response.status_code,
                latency_ms=response.elapsed_ms,
                attempts_made=response.attempts_made,
            )
            log_json(
                logger,
                logging.INFO,
                "tool_call_succeeded",
                tool_name=tool.tool_meta.name,
                downstream_status=response.status_code,
                latency_ms=response.elapsed_ms,
                attempts_made=response.attempts_made,
            )
            return result
        except ValidationError as exc:
            await self._record_failure(tool.tool_meta.name, "VALIDATION_ERROR", None, 0)
            raise from_validation_error(exc) from exc
        except GatewayError as exc:
            await self._record_failure(
                tool.tool_meta.name,
                exc.category,
                exc.data.get("downstream_status"),
                int(exc.data.get("elapsed_ms", 0)),
            )
            raise
        except httpx.TimeoutException as exc:
            mapped = from_timeout(exc)
            await self._record_failure(tool.tool_meta.name, mapped.category, None, 0)
            raise mapped from exc
        except httpx.RequestError as exc:
            mapped = from_request_error(exc)
            await self._record_failure(tool.tool_meta.name, mapped.category, None, 0)
            raise mapped from exc
        except Exception as exc:
            mapped = from_unknown_exception(exc)
            await self._record_failure(tool.tool_meta.name, mapped.category, None, 0)
            raise mapped from exc

    async def _record_failure(
        self,
        tool_name: str,
        error_type: str,
        downstream_status: int | None,
        latency_ms: int,
    ) -> None:
        await self.access_log_repository.record_call(
            request_id=get_request_id(),
            tool_name=tool_name,
            success=False,
            error_type=error_type,
            downstream_status=downstream_status,
            latency_ms=latency_ms,
            attempts_made=1,
        )
        log_json(
            logger,
            logging.WARNING,
            "tool_call_failed",
            tool_name=tool_name,
            error_type=error_type,
            downstream_status=downstream_status,
            latency_ms=latency_ms,
        )

    def _build_request(self, tool: ToolConfig, arguments: dict[str, Any]) -> PreparedRestRequest:
        mapping = tool.request_mapping
        path = tool.http_target.path
        for arg_name, path_key in mapping.path_map.items():
            if arg_name not in arguments or arguments[arg_name] is None:
                raise gateway_error(
                    "VALIDATION_ERROR",
                    f"Missing required path argument '{arg_name}'.",
                    data={"argument": arg_name, "target": path_key},
                )
            path = path.replace(f"{{{path_key}}}", str(arguments[arg_name]))

        unresolved_path_variables = _PATH_VARIABLE_PATTERN.findall(path)
        if unresolved_path_variables:
            raise gateway_error(
                "INTERNAL_ERROR",
                "Path template contains unresolved variables.",
                data={
                    "path_template": tool.http_target.path,
                    "resolved_path": path,
                    "unresolved_variables": unresolved_path_variables,
                },
            )

        params = {
            target_name: arguments[arg_name]
            for arg_name, target_name in mapping.query_map.items()
            if arg_name in arguments and arguments[arg_name] is not None
        }
        headers = {
            target_name: str(arguments[arg_name])
            for arg_name, target_name in mapping.header_map.items()
            if arg_name in arguments and arguments[arg_name] is not None
        }
        headers.update(mapping.constant_headers)

        body: dict[str, Any] = {}
        for arg_name, body_path in mapping.body_map.items():
            if arg_name in arguments and arguments[arg_name] is not None:
                _set_nested(body, body_path, arguments[arg_name])

        method = tool.http_target.method.upper()
        idempotent = (
            tool.http_target.idempotent
            if tool.http_target.idempotent is not None
            else method in {"GET", "DELETE"}
        )
        return PreparedRestRequest(
            method=method,
            url=f"{tool.http_target.base_url.rstrip('/')}{path}",
            headers=headers,
            params=params,
            json_body=body or None,
            timeout_seconds=tool.http_target.timeout_seconds,
            retry_count=tool.http_target.retry_count,
            idempotent=idempotent,
        )
