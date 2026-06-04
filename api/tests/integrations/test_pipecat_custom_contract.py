"""Integration coverage for Dograh's custom Pipecat runtime contract."""

import asyncio

import pytest
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.tests import MockLLMService, MockTTSService
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams

from api.enums import WorkflowRunMode
from api.services.pipecat.audio_config import AudioConfig, create_audio_config
from api.services.pipecat.pipeline_builder import create_pipeline_task


def test_pipecat_test_doubles_are_exported_from_package():
    """Dograh integration tests import Pipecat test doubles from pipecat.tests."""
    assert MockLLMService.__name__ == "MockLLMService"
    assert MockTTSService.__name__ == "MockTTSService"


@pytest.mark.asyncio
async def test_create_pipeline_task_accepts_dograh_tracing_contract():
    """Dograh call setup passes custom tracing kwargs into PipelineTask."""
    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )
    pipeline = Pipeline([transport.input(), transport.output()])

    task = create_pipeline_task(
        pipeline,
        workflow_run_id=4242,
        audio_config=create_audio_config(WorkflowRunMode.SMALLWEBRTC.value),
        conversation_parent_context=None,
        conversation_type="voice",
        additional_span_attributes={"dograh.test": "pipecat-custom-contract"},
    )

    assert hasattr(task, "user_bot_latency_observer")
    assert task.user_bot_latency_observer is not None

    runner = PipelineRunner()
    run_task = asyncio.create_task(runner.run(task))
    try:
        await asyncio.wait_for(task._pipeline_start_event.wait(), timeout=3.0)
    finally:
        await task.cancel()
        await asyncio.wait_for(run_task, timeout=5.0)


def test_create_pipeline_task_accepts_sip_audio_contract():
    """SIP calls build an 8 kHz pipeline task before running the session."""
    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )
    pipeline = Pipeline([transport.input(), transport.output()])

    task = create_pipeline_task(
        pipeline,
        workflow_run_id=4243,
        audio_config=AudioConfig(
            transport_in_sample_rate=8000,
            transport_out_sample_rate=8000,
            vad_sample_rate=8000,
            pipeline_sample_rate=8000,
        ),
    )

    assert task.params.audio_in_sample_rate == 8000
    assert task.params.audio_out_sample_rate == 8000
    assert task.user_bot_latency_observer is not None
