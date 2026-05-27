"""Tests for the built-in TVox callback tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from api.routes.tool import (
    CreateToolRequest,
    TvoxCallbackToolDefinition,
    UpdateToolRequest,
)
from api.services.workflow.tools import tvox_callback
from api.services.workflow.tools.tvox_callback import (
    build_tvox_callback_body,
    execute_tvox_callback_tool,
)


@dataclass
class MockToolModel:
    tool_uuid: str = "tvox-callback-uuid"
    name: str = "TVox Callback"
    description: str = "Return data to TVox"
    category: str = "tvox_callback"
    definition: dict[str, Any] | None = None


def _tool(config: dict[str, Any] | None = None) -> MockToolModel:
    return MockToolModel(
        definition={
            "schema_version": 1,
            "type": "tvox_callback",
            "config": config or {},
        }
    )


def test_create_tool_request_accepts_tvox_callback_definition():
    req = CreateToolRequest(
        name="TVox Callback",
        description="Return data to TVox",
        category="tvox_callback",
        definition={
            "schema_version": 1,
            "type": "tvox_callback",
            "config": {
                "url": None,
                "credential_uuid": None,
                "timeout_ms": 10000,
                "end_call_on_success": True,
                "parameters": [
                    {
                        "name": "outcome",
                        "type": "string",
                        "description": "Call outcome",
                        "required": True,
                    }
                ],
            },
        },
    )

    assert isinstance(req.definition, TvoxCallbackToolDefinition)
    assert req.definition.type == "tvox_callback"
    assert req.definition.config.url is None
    assert req.definition.config.timeout_ms == 10000
    assert req.definition.config.end_call_on_success is True
    assert req.definition.config.parameters[0].name == "outcome"


def test_update_tool_request_accepts_tvox_callback_without_url():
    req = UpdateToolRequest(
        definition={
            "schema_version": 1,
            "type": "tvox_callback",
            "config": {"parameters": []},
        }
    )

    assert isinstance(req.definition, TvoxCallbackToolDefinition)
    assert req.definition.config.url is None
    assert req.definition.config.timeout_ms == 10000


def test_build_payload_uses_telenia_headers_first():
    payload = build_tvox_callback_body(
        values={"outcome": "ok"},
        initial_context={
            "sip_headers": {
                "X-Telenia-Callid": "call-from-header",
                "x-telenia-operationid": "operation-from-header",
            },
            "sip_call_id": "sip-fallback",
            "caller_number": "+3901",
            "called_number": "+3902",
        },
        gathered_context={"call_id": "gathered-fallback"},
        workflow_run_id=123,
        workflow_id=45,
    )

    assert payload["values"] == {"outcome": "ok"}
    assert payload["callId"] == "call-from-header"
    assert payload["operationId"] == "operation-from-header"
    assert payload["workflowRunId"] == 123
    assert payload["workflowId"] == 45
    assert payload["callerNumber"] == "+3901"
    assert payload["calledNumber"] == "+3902"
    assert payload["sipCallId"] == "sip-fallback"


def test_build_payload_falls_back_to_sip_call_and_run_id():
    payload = build_tvox_callback_body(
        values={},
        initial_context={"sip_call_id": "sip-call-id"},
        gathered_context={"call_id": "gathered-call-id"},
        workflow_run_id=987,
        workflow_id=None,
    )

    assert payload["callId"] == "sip-call-id"
    assert payload["operationId"] == "987"


def test_build_payload_falls_back_to_gathered_call_id():
    payload = build_tvox_callback_body(
        values={},
        initial_context={},
        gathered_context={"call_id": "gathered-call-id"},
        workflow_run_id=None,
        workflow_id=None,
    )

    assert payload["callId"] == "gathered-call-id"
    assert payload["operationId"] is None


@pytest.mark.asyncio
async def test_execute_success_posts_payload_and_ends_call():
    tool = _tool(
        {
            "url": "https://tvox.example.com/workflow/hook/call",
            "timeout_ms": 3000,
            "end_call_on_success": True,
        }
    )

    mock_response = Mock()
    mock_response.status_code = 204
    mock_response.json.side_effect = ValueError()
    mock_response.text = ""

    with (
        patch.object(
            tvox_callback.db_client,
            "list_telephony_configurations_by_provider",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            tvox_callback.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=Mock(workflow_id=45),
        ),
        patch(
            "api.services.workflow.tools.tvox_callback.httpx.AsyncClient"
        ) as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await execute_tvox_callback_tool(
            tool=tool,
            arguments={"outcome": "ok"},
            initial_context={"sip_call_id": "sip-call-id"},
            gathered_context={},
            organization_id=1,
            workflow_run_id=123,
        )

    assert result["status"] == "success"
    assert result["status_code"] == 204
    assert result["end_call"] is True
    call_kwargs = mock_client.post.call_args.kwargs
    assert call_kwargs["json"]["values"] == {"outcome": "ok"}
    assert call_kwargs["json"]["callId"] == "sip-call-id"
    assert call_kwargs["json"]["workflowId"] == 45


@pytest.mark.asyncio
async def test_execute_non_2xx_does_not_end_call():
    tool = _tool({"url": "https://tvox.example.com/fail"})
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.json.return_value = {"error": "boom"}

    with (
        patch.object(
            tvox_callback.db_client,
            "list_telephony_configurations_by_provider",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            tvox_callback.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "api.services.workflow.tools.tvox_callback.httpx.AsyncClient"
        ) as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await execute_tvox_callback_tool(
            tool=tool,
            arguments={},
            initial_context={},
            gathered_context={},
            organization_id=1,
            workflow_run_id=123,
        )

    assert result["status"] == "error"
    assert result["status_code"] == 500
    assert result["end_call"] is False


@pytest.mark.asyncio
async def test_execute_timeout_does_not_end_call():
    tool = _tool({"url": "https://tvox.example.com/slow"})

    with (
        patch.object(
            tvox_callback.db_client,
            "list_telephony_configurations_by_provider",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            tvox_callback.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "api.services.workflow.tools.tvox_callback.httpx.AsyncClient"
        ) as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("slow")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await execute_tvox_callback_tool(
            tool=tool,
            arguments={},
            initial_context={},
            gathered_context={},
            organization_id=1,
            workflow_run_id=123,
        )

    assert result["status"] == "error"
    assert result["error"] == "Request timed out"
    assert result["end_call"] is False


@pytest.mark.asyncio
async def test_execute_missing_url_in_non_local_environment_does_not_end_call():
    with (
        patch.object(tvox_callback, "ENVIRONMENT", "production"),
        patch.object(
            tvox_callback.db_client,
            "list_telephony_configurations_by_provider",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        result = await execute_tvox_callback_tool(
            tool=_tool(),
            arguments={},
            initial_context={},
            gathered_context={},
            organization_id=1,
            workflow_run_id=123,
        )

    assert result == {
        "status": "error",
        "error": "TVox callback URL is not configured",
        "end_call": False,
    }


@pytest.mark.asyncio
async def test_execute_uses_provider_url_and_credential_when_tool_omits_them():
    provider_config = Mock(
        credentials={
            "callback_url": "https://provider.example.com/hook",
            "callback_credential_uuid": "cred-uuid",
        }
    )
    credential = Mock()
    credential.credential_type = "bearer_token"
    credential.credential_data = {"token": "secret-token"}
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": True}

    with (
        patch.object(
            tvox_callback.db_client,
            "list_telephony_configurations_by_provider",
            new_callable=AsyncMock,
            return_value=[provider_config],
        ),
        patch.object(
            tvox_callback.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            tvox_callback.db_client,
            "get_credential_by_uuid",
            new_callable=AsyncMock,
            return_value=credential,
        ) as mock_get_credential,
        patch(
            "api.services.workflow.tools.tvox_callback.httpx.AsyncClient"
        ) as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await execute_tvox_callback_tool(
            tool=_tool(),
            arguments={},
            initial_context={},
            gathered_context={},
            organization_id=1,
            workflow_run_id=123,
        )

    assert result["status"] == "success"
    mock_get_credential.assert_awaited_once_with("cred-uuid", 1)
    call_args = mock_client.post.call_args
    assert call_args.args[0] == "https://provider.example.com/hook"
    assert call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}
