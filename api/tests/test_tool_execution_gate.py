"""Tests for per-node tool execution gating and tool-wait recordings.

Covers the engine-side behavior behind the node fields
``tool_execution_mode`` ("async" | "post_speech") and
``tool_wait_recording_id``:

- ``_tool_execution_gate`` defers tool execution until the bot finishes
  speaking only when the current node opts into post_speech mode.
- ``wrap_tool_handler_with_wait_audio`` loops the node's recording while the
  wrapped handler runs and stops it as soon as the handler returns.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import api.services.workflow.pipecat_engine as engine_mod
from api.services.workflow.pipecat_engine import PipecatEngine


def _engine() -> PipecatEngine:
    return PipecatEngine(workflow=MagicMock(), call_context_vars={})


def _node(**overrides) -> SimpleNamespace:
    defaults = dict(
        name="agent",
        tool_execution_mode=None,
        tool_wait_recording_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _gate_params() -> SimpleNamespace:
    return SimpleNamespace(
        function_name="my_tool", tool_call_id="call_1", arguments={}
    )


# ─────────────────────────────────────────────────────────────────────────
# _tool_execution_gate
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_noop_in_async_mode_while_bot_speaking():
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="async")
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = True

    # Must return immediately despite the bot speaking.
    await asyncio.wait_for(engine._tool_execution_gate(_gate_params()), timeout=0.5)


@pytest.mark.asyncio
async def test_gate_noop_when_mode_unset():
    engine = _engine()
    engine._current_node = _node(tool_execution_mode=None)
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = True

    await asyncio.wait_for(engine._tool_execution_gate(_gate_params()), timeout=0.5)


@pytest.mark.asyncio
async def test_gate_noop_without_speech_tracking():
    """Text chat has no mute strategy wired, so post_speech must not wait."""
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="post_speech")
    engine._speech_state_tracking_active = False
    engine._bot_is_speaking = True

    await asyncio.wait_for(engine._tool_execution_gate(_gate_params()), timeout=0.5)


@pytest.mark.asyncio
async def test_gate_waits_until_bot_stops_speaking(monkeypatch):
    monkeypatch.setattr(engine_mod, "TOOL_GATE_MAX_SPEECH_WAIT_SECS", 5.0)
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="post_speech")
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = True

    gate_task = asyncio.create_task(engine._tool_execution_gate(_gate_params()))
    await asyncio.sleep(0.2)
    assert not gate_task.done(), "gate must hold while the bot is speaking"

    engine._bot_is_speaking = False
    await asyncio.wait_for(gate_task, timeout=1.0)


@pytest.mark.asyncio
async def test_gate_waits_for_queued_speech(monkeypatch):
    """Queued speech (transition speech / tool messages) also holds the gate."""
    monkeypatch.setattr(engine_mod, "TOOL_GATE_MAX_SPEECH_WAIT_SECS", 5.0)
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="post_speech")
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = False
    engine._queued_speech_mute_state = "waiting"

    gate_task = asyncio.create_task(engine._tool_execution_gate(_gate_params()))
    await asyncio.sleep(0.2)
    assert not gate_task.done(), "gate must hold while speech is queued"

    engine._queued_speech_mute_state = "idle"
    await asyncio.wait_for(gate_task, timeout=1.0)


@pytest.mark.asyncio
async def test_gate_releases_after_grace_when_bot_never_speaks(monkeypatch):
    """If TTS never starts (e.g. tool-only response), the grace period expires
    and the tool executes anyway."""
    monkeypatch.setattr(engine_mod, "TOOL_GATE_SPEECH_START_GRACE_SECS", 0.15)
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="post_speech")
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = False

    await asyncio.wait_for(engine._tool_execution_gate(_gate_params()), timeout=1.0)


@pytest.mark.asyncio
async def test_gate_releases_when_call_disposed(monkeypatch):
    monkeypatch.setattr(engine_mod, "TOOL_GATE_MAX_SPEECH_WAIT_SECS", 5.0)
    engine = _engine()
    engine._current_node = _node(tool_execution_mode="post_speech")
    engine._speech_state_tracking_active = True
    engine._bot_is_speaking = True

    gate_task = asyncio.create_task(engine._tool_execution_gate(_gate_params()))
    await asyncio.sleep(0.1)
    assert not gate_task.done()

    engine._call_disposed = True
    await asyncio.wait_for(gate_task, timeout=1.0)


# ─────────────────────────────────────────────────────────────────────────
# wrap_tool_handler_with_wait_audio
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrapper_passthrough_without_recording():
    engine = _engine()
    engine._current_node = _node()
    engine._speech_state_tracking_active = True

    calls = []

    async def handler(params):
        calls.append(params)

    wrapped = engine.wrap_tool_handler_with_wait_audio(handler)
    params = SimpleNamespace()
    await wrapped(params)

    assert calls == [params]


@pytest.mark.asyncio
async def test_wrapper_passthrough_without_speech_tracking():
    """Text chat: recording configured but no voice pipeline — no playback."""
    engine = _engine()
    engine._current_node = _node(tool_wait_recording_id="7")
    engine._speech_state_tracking_active = False
    engine._fetch_recording_audio = AsyncMock()

    calls = []

    async def handler(params):
        calls.append(params)

    wrapped = engine.wrap_tool_handler_with_wait_audio(handler)
    await wrapped(SimpleNamespace())

    assert len(calls) == 1
    engine._fetch_recording_audio.assert_not_called()


@pytest.mark.asyncio
async def test_wrapper_plays_recording_during_slow_tool():
    engine = _engine()
    engine._current_node = _node(tool_wait_recording_id="7")
    engine._speech_state_tracking_active = True

    # 0.5s of PCM-16 silence at 16kHz
    audio = b"\x00\x00" * 8000
    engine._fetch_recording_audio = AsyncMock(
        return_value=SimpleNamespace(audio=audio, transcript=None)
    )

    queued = []

    async def queue_frame(frame):
        queued.append(frame)

    engine._transport_output = SimpleNamespace(queue_frame=queue_frame)

    async def slow_handler(params):
        # Longer than play_audio_data_loop's 0.5s start delay
        await asyncio.sleep(0.9)

    wrapped = engine.wrap_tool_handler_with_wait_audio(slow_handler)
    await wrapped(SimpleNamespace())

    engine._fetch_recording_audio.assert_awaited_once_with(recording_pk=7)
    assert queued, "recording frames must be queued while the tool runs"

    # Playback is stopped once the handler returns: no frames trickle after.
    count = len(queued)
    await asyncio.sleep(0.3)
    assert len(queued) == count


@pytest.mark.asyncio
async def test_wrapper_skips_playback_for_fast_tool():
    """Tools faster than the start delay produce no audio at all."""
    engine = _engine()
    engine._current_node = _node(tool_wait_recording_id="7")
    engine._speech_state_tracking_active = True

    audio = b"\x00\x00" * 8000
    engine._fetch_recording_audio = AsyncMock(
        return_value=SimpleNamespace(audio=audio, transcript=None)
    )

    queued = []

    async def queue_frame(frame):
        queued.append(frame)

    engine._transport_output = SimpleNamespace(queue_frame=queue_frame)

    async def fast_handler(params):
        await asyncio.sleep(0.05)

    wrapped = engine.wrap_tool_handler_with_wait_audio(fast_handler)
    await wrapped(SimpleNamespace())

    assert queued == []


@pytest.mark.asyncio
async def test_wrapper_survives_fetch_failure():
    engine = _engine()
    engine._current_node = _node(tool_wait_recording_id="7")
    engine._speech_state_tracking_active = True
    engine._fetch_recording_audio = AsyncMock(side_effect=RuntimeError("s3 down"))
    engine._transport_output = SimpleNamespace(queue_frame=AsyncMock())

    calls = []

    async def handler(params):
        calls.append(params)

    wrapped = engine.wrap_tool_handler_with_wait_audio(handler)
    await wrapped(SimpleNamespace())

    assert len(calls) == 1
