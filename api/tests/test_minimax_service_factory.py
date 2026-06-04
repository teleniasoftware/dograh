from types import SimpleNamespace
from unittest.mock import patch

from api.services.configuration.registry import (
    MiniMaxTTSConfiguration,
    ServiceProviders,
)
from api.services.pipecat.service_factory import create_tts_service


class TestMiniMaxTTSConfiguration:
    def test_default_values(self):
        config = MiniMaxTTSConfiguration(api_key="test-key", group_id="test-group")
        assert config.provider == ServiceProviders.MINIMAX
        assert config.model == "speech-2.8-hd"
        assert config.voice == "English_Graceful_Lady"
        assert config.speed == 1.0
        assert config.group_id == "test-group"


class TestMiniMaxTTSServiceFactory:
    def test_create_minimax_tts_service(self):
        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider=ServiceProviders.MINIMAX.value,
                api_key="test-key",
                model="speech-2.8-hd",
                voice="English_Graceful_Lady",
                speed=1.0,
                base_url="https://api.minimax.io/v1",
                group_id="test-group",
            )
        )
        audio_config = SimpleNamespace(transport_in_sample_rate=16000)

        with (
            patch("api.services.pipecat.service_factory.aiohttp.ClientSession"),
            patch(
                "api.services.pipecat.service_factory.MiniMaxOwnedSessionTTSService"
            ) as mock_service,
        ):
            create_tts_service(user_config, audio_config)

        assert mock_service.call_count == 1
        kwargs = mock_service.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["group_id"] == "test-group"
        assert kwargs["settings"].model == "speech-2.8-hd"
        assert kwargs["settings"].voice == "English_Graceful_Lady"
        assert kwargs["settings"].speed == 1.0
        assert kwargs["aiohttp_session"] is not None
