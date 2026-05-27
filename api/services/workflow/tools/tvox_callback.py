"""Built-in TVox callback tool execution."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from loguru import logger

from api.constants import ENVIRONMENT
from api.db import db_client
from api.enums import Environment
from api.utils.credential_auth import build_auth_header
from api.utils.url_security import validate_user_configured_service_url

DEV_TVOX_CALLBACK_URL = "http://host.containers.internal:8075/workflow/hook/call"


def _headers_get(headers: dict[str, Any], name: str) -> Any:
    normalized = name.lower()
    for key, value in headers.items():
        if str(key).lower() == normalized:
            return value
    return None


def build_tvox_callback_body(
    *,
    values: dict[str, Any],
    initial_context: Optional[dict[str, Any]],
    gathered_context: Optional[dict[str, Any]],
    workflow_run_id: Optional[int],
    workflow_id: Optional[int],
) -> dict[str, Any]:
    """Build the legacy-compatible TVox payload plus Dograh run metadata."""

    initial = dict(initial_context or {})
    gathered = dict(gathered_context or {})
    sip_headers = initial.get("sip_headers") or {}
    if not isinstance(sip_headers, dict):
        sip_headers = {}

    call_id = (
        _headers_get(sip_headers, "x-telenia-callid")
        or initial.get("sip_call_id")
        or gathered.get("call_id")
    )
    operation_id = _headers_get(sip_headers, "x-telenia-operationid") or (
        str(workflow_run_id) if workflow_run_id is not None else None
    )

    return {
        "values": dict(values or {}),
        "callId": call_id,
        "operationId": operation_id,
        "workflowRunId": workflow_run_id,
        "workflowId": workflow_id,
        "callerNumber": initial.get("caller_number"),
        "calledNumber": initial.get("called_number"),
        "sipCallId": initial.get("sip_call_id"),
        "initialContext": initial,
        "gatheredContext": gathered,
    }


async def _get_provider_callback_config(
    organization_id: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    if organization_id is None:
        return None, None

    configs = await db_client.list_telephony_configurations_by_provider(
        organization_id, "sip"
    )
    if not configs:
        return None, None

    credentials = configs[0].credentials or {}
    return credentials.get("callback_url"), credentials.get("callback_credential_uuid")


async def execute_tvox_callback_tool(
    *,
    tool: Any,
    arguments: Dict[str, Any],
    initial_context: Optional[Dict[str, Any]],
    gathered_context: Optional[Dict[str, Any]],
    organization_id: Optional[int],
    workflow_run_id: Optional[int],
) -> Dict[str, Any]:
    """POST a TVox callback payload and return a structured execution result."""

    definition = tool.definition or {}
    config = definition.get("config", {}) or {}

    provider_url, provider_credential_uuid = await _get_provider_callback_config(
        organization_id
    )
    url = (config.get("url") or "").strip() or provider_url
    credential_uuid = config.get("credential_uuid") or provider_credential_uuid

    if not url and ENVIRONMENT == Environment.LOCAL.value:
        url = DEV_TVOX_CALLBACK_URL

    if not url:
        return {
            "status": "error",
            "error": "TVox callback URL is not configured",
            "end_call": False,
        }

    try:
        validate_user_configured_service_url(url, field_name="tvox_callback_url")
    except ValueError as e:
        return {"status": "error", "error": str(e), "end_call": False}

    workflow_id = None
    if workflow_run_id is not None:
        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        workflow_id = getattr(workflow_run, "workflow_id", None) if workflow_run else None

    body = build_tvox_callback_body(
        values=arguments or {},
        initial_context=initial_context,
        gathered_context=gathered_context,
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
    )

    headers: dict[str, str] = {}
    if credential_uuid and organization_id:
        credential = await db_client.get_credential_by_uuid(
            credential_uuid, organization_id
        )
        if credential:
            headers.update(build_auth_header(credential))
        else:
            logger.warning(
                "TVox callback credential {} not found for organization {}",
                credential_uuid,
                organization_id,
            )

    timeout_seconds = float(config.get("timeout_ms", 10000)) / 1000
    logger.info("Executing TVox callback tool '{}' -> {}", tool.name, url)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        return {"status": "error", "error": "Request timed out", "end_call": False}
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"HTTP error: {e}", "end_call": False}

    try:
        response_data: Any = response.json()
    except ValueError:
        response_data = response.text

    success = 200 <= response.status_code < 300
    return {
        "status": "success" if success else "error",
        "status_code": response.status_code,
        "data": response_data,
        "payload": body,
        "end_call": bool(success and config.get("end_call_on_success", True)),
    }
