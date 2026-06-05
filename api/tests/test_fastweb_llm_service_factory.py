from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.services.configuration.registry import FastwebLLMConfiguration, ServiceProviders
from api.services.pipecat.service_factory import create_llm_service
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.fastweb.llm import FastwebLLMService, FastwebLLMSettings


def test_fastweb_llm_configuration_requires_base_url():
    with pytest.raises(ValidationError):
        FastwebLLMConfiguration(api_key="fastweb-key")


def test_create_fastweb_llm_service_passes_configured_values():
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

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "fastweb-key"
    assert kwargs["base_url"] == "https://fastwebai-agents-api.fastedge.it"
    assert kwargs["release_tag"] == "LATEST"
    assert kwargs["settings"].model == "agent-centralino"


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
