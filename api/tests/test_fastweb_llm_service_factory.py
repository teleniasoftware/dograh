from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.services.configuration.registry import FastwebLLMConfiguration, ServiceProviders
from api.services.pipecat.service_factory import create_llm_service
from pipecat.frames.frames import FunctionCallFromLLM
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.fastweb.llm import FastwebLLMService, FastwebLLMSettings


def test_fastweb_llm_configuration_requires_base_url():
    with pytest.raises(ValidationError):
        FastwebLLMConfiguration(api_key="fastweb-key")


def test_fastweb_llm_configuration_routing_defaults():
    config = FastwebLLMConfiguration(
        api_key="fastweb-key",
        base_url="https://fastwebai-agents-api.fastedge.it",
    )

    assert config.routing_tool_name is None
    assert config.routing_output_name == "routing"
    assert config.routing_argument_name == "routing"


def test_create_fastweb_llm_service_passes_configured_values():
    user_config = SimpleNamespace(
        llm=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key="fastweb-key",
            model="agent-centralino",
            base_url="https://fastwebai-agents-api.fastedge.it",
            release_tag="LATEST",
            routing_tool_name="route_handler",
            routing_output_name="routing",
            routing_argument_name="route",
        )
    )

    with patch("api.services.pipecat.service_factory.FastwebLLMService") as mock_service:
        create_llm_service(user_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "fastweb-key"
    assert kwargs["base_url"] == "https://fastwebai-agents-api.fastedge.it"
    assert kwargs["release_tag"] == "LATEST"
    assert kwargs["settings"].model == "agent-centralino"
    assert kwargs["settings"].routing_tool_name == "route_handler"
    assert kwargs["settings"].routing_output_name == "routing"
    assert kwargs["settings"].routing_argument_name == "route"


def test_create_fastweb_llm_service_uses_routing_defaults():
    user_config = SimpleNamespace(
        llm=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key="fastweb-key",
            model="agent-centralino",
            base_url="https://fastwebai-agents-api.fastedge.it",
            release_tag="LATEST",
        )
    )

    with patch("api.services.pipecat.service_factory.FastwebLLMService") as mock_service:
        create_llm_service(user_config)

    settings = mock_service.call_args.kwargs["settings"]
    assert settings.routing_tool_name is None
    assert settings.routing_output_name == "routing"
    assert settings.routing_argument_name == "routing"


def test_create_fastweb_llm_service_requires_base_url():
    user_config = SimpleNamespace(
        llm=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key="fastweb-key",
            model="agent-centralino",
            base_url=None,
            release_tag="LATEST",
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        create_llm_service(user_config)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "base_url is required for FastWeb LLM provider"


def test_create_fastweb_llm_service_requires_api_key():
    user_config = SimpleNamespace(
        llm=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key=None,
            model="agent-centralino",
            base_url="https://fastwebai-agents-api.fastedge.it",
            release_tag="LATEST",
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        create_llm_service(user_config)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "api_key is required for FastWeb LLM provider"


def test_fastweb_llm_service_maps_context_and_response_outputs():
    service = FastwebLLMService(
        api_key="fastweb-key",
        base_url="https://fastwebai-agents-api.fastedge.it",
        settings=FastwebLLMSettings(model="agent-centralino"),
    )
    context = LLMContext(
        messages=[
            {"role": "system", "content": "ignored by Fastweb workflow input"},
            {"role": "user", "content": "Ho bisogno di una mano."},
            {"role": "assistant", "content": [{"type": "text", "text": "Dimmi pure."}]},
        ]
    )

    assert (
        service._execute_workflow_url()
        == "https://fastwebai-agents-api.fastedge.it/predict/v1/execute-workflow"
    )
    assert service._chat_history_from_context(context) == [
        {"role": "USER", "text": "Ho bisogno di una mano."},
        {"role": "ASSISTANT", "text": "Dimmi pure."},
    ]
    assert (
        service._extract_output_value(
            {
                "data": {
                    "outputs": [
                        {"name": "routing", "value": "ROUTE_FINANCE"},
                        {"name": "communication", "value": "Ti passo al team finance."},
                    ]
                }
            },
            "communication",
        )
        == "Ti passo al team finance."
    )


@pytest.mark.asyncio
async def test_fastweb_llm_service_does_not_call_tool_without_routing_output():
    service = FastwebLLMService(
        api_key="fastweb-key",
        base_url="https://fastwebai-agents-api.fastedge.it",
        settings=FastwebLLMSettings(
            model="agent-centralino",
            routing_tool_name="route_handler",
        ),
    )
    context = LLMContext(messages=[{"role": "user", "content": "Ciao"}])
    service.start_ttfb_metrics = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    service._execute_workflow = AsyncMock(
        return_value={
            "data": {
                "outputs": [
                    {"name": "communication", "value": "Di cosa hai bisogno?"}
                ]
            }
        }
    )
    service._push_llm_text = AsyncMock()
    service.run_function_calls = AsyncMock()

    await service._process_context(context)

    service._push_llm_text.assert_awaited_once_with("Di cosa hai bisogno?")
    service.run_function_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_fastweb_llm_service_routes_metadata_to_configured_tool():
    service = FastwebLLMService(
        api_key="fastweb-key",
        base_url="https://fastwebai-agents-api.fastedge.it",
        settings=FastwebLLMSettings(
            model="agent-centralino",
            routing_tool_name="route_handler",
            routing_output_name="routing",
            routing_argument_name="route",
        ),
    )
    context = LLMContext(messages=[{"role": "user", "content": "Finance"}])
    service.start_ttfb_metrics = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    service._execute_workflow = AsyncMock(
        return_value={
            "data": {
                "outputs": [
                    {
                        "name": "communication",
                        "value": "Ti passo al team finance.",
                    },
                    {"name": "routing", "value": "ROUTE_FINANCE"},
                ]
            }
        }
    )
    service._push_llm_text = AsyncMock()
    service.run_function_calls = AsyncMock()

    await service._process_context(context)

    service._push_llm_text.assert_awaited_once_with("Ti passo al team finance.")
    service.run_function_calls.assert_awaited_once()
    function_calls = service.run_function_calls.await_args.args[0]
    assert len(function_calls) == 1
    function_call = function_calls[0]
    assert function_call.function_name == "route_handler"
    assert function_call.tool_call_id.startswith("fastweb-routing-")
    assert function_call.arguments == {"route": "ROUTE_FINANCE"}
    assert function_call.context is context
    assert getattr(function_call, "run_llm") is False


@pytest.mark.asyncio
async def test_fastweb_routing_run_llm_flag_reaches_function_runner():
    service = FastwebLLMService(
        api_key="fastweb-key",
        base_url="https://fastwebai-agents-api.fastedge.it",
        settings=FastwebLLMSettings(model="agent-centralino"),
    )
    context = LLMContext(messages=[{"role": "user", "content": "Tecnica"}])
    function_call = FunctionCallFromLLM(
        function_name="fastweb_callback",
        tool_call_id="fastweb-routing-test",
        arguments={"routing": "ROUTE_TECH"},
        context=context,
    )
    setattr(function_call, "run_llm", False)
    service._call_event_handler = AsyncMock()
    service.push_frame = AsyncMock()
    service.broadcast_frame = AsyncMock()
    service._run_parallel_function_calls = AsyncMock()

    await service.run_function_calls([function_call])

    service._run_parallel_function_calls.assert_awaited_once()
    runner_items = service._run_parallel_function_calls.await_args.args[0]
    assert len(runner_items) == 1
    assert runner_items[0].function_name == "fastweb_callback"
    assert runner_items[0].run_llm is False
