from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pipecat.transcriptions.language import Language

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.service_factory import create_stt_service


def test_create_google_stt_service_uses_credentials_location_and_language():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials='{"project_id":"demo-project"}',
            api_key=None,
            model="latest_long",
            language="en-US",
            location="us-central1",
        )
    )
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with patch("api.services.pipecat.service_factory.GoogleSTTService") as mock_service:
        create_stt_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["credentials"] == '{"project_id":"demo-project"}'
    assert kwargs["location"] == "us-central1"
    assert kwargs["sample_rate"] == 16000
    assert kwargs["settings"].model == "latest_long"
    assert kwargs["settings"].languages == [Language.EN_US]


def test_create_google_stt_service_falls_back_to_raw_language_codes():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials=None,
            api_key=None,
            model="chirp_3",
            language="cmn-Hans-CN",
            location="global",
        )
    )
    audio_config = SimpleNamespace(transport_in_sample_rate=24000)

    with patch("api.services.pipecat.service_factory.GoogleSTTService") as mock_service:
        create_stt_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["sample_rate"] == 24000
    assert kwargs["settings"].model == "chirp_3"
    assert kwargs["settings"].language_codes == ["cmn-Hans-CN"]


def test_create_fastweb_stt_service_passes_api_key_and_language():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key="fastweb-stt-key",
            model="default",
            language="it",
            base_url="wss://bpod1.ai-factory.fastweb.it",
        )
    )
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with patch("api.services.pipecat.service_factory.FastwebSTTService") as mock_service:
        create_stt_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs == {
        "api_key": "fastweb-stt-key",
        "base_url": "wss://bpod1.ai-factory.fastweb.it",
        "language": "it",
        "sample_rate": 16000,
    }


def test_create_fastweb_stt_service_requires_api_key():
    user_config = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.FASTWEB.value,
            api_key=None,
            model="default",
            language="it",
            base_url="wss://bpod1.ai-factory.fastweb.it",
        )
    )
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with pytest.raises(HTTPException) as exc_info:
        create_stt_service(user_config, audio_config)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "api_key is required for FastWeb STT provider"


def test_validator_requires_fastweb_stt_api_key():
    validator = UserConfigurationValidator()
    config = SimpleNamespace(
        provider=ServiceProviders.FASTWEB.value,
        api_key=None,
        base_url="wss://bpod1.ai-factory.fastweb.it",
    )

    assert validator._validate_service(config, "stt") == [
        {
            "model": "stt",
            "message": "API key is required for fastweb STT",
        }
    ]
