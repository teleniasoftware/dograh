"""
SIP/RTP Pipecat Transport

Wraps RTPSession to provide a Pipecat-compatible input/output transport pair.

Audio path:
  Inbound:  RTP UDP -> G.711 decode (RTPSession) -> PCM queue -> InputAudioRawFrame
  Outbound: OutputAudioRawFrame -> PCM -> G.711 encode (RTPSession) -> RTP UDP
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import numpy as np
import soxr
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    EndTaskFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSStoppedFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams


from .rtp_session import DTMFEvent, RTPSession

logger = logging.getLogger(__name__)

_FRAME_MS = 20


def _frame_size_for_rate(sample_rate: int) -> int:
    """Calculate frame size in bytes for given sample rate and frame duration."""
    return sample_rate * _FRAME_MS // 1000 * 2


class SIPInputTransport(BaseInputTransport):
    """
    Pipecat input transport for SIP/RTP calls.

    Receives decoded PCM audio from RTPSession (via an asyncio Queue) and
    emits InputAudioRawFrame into the Pipecat pipeline.
    """

    def __init__(
        self,
        rtp_session: RTPSession,
        sample_rate: int,
        on_dtmf_received: Optional[Callable[[str], Awaitable[None]]] = None,
        on_connected: Optional[Callable[[], Awaitable[None]]] = None,
        input_cooldown_after_tts_secs: float = 0.5,
        name: str = "SIPInputTransport",
        **kwargs,
    ):
        kwargs.setdefault(
            "params",
            TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
                audio_in_sample_rate=sample_rate,
            ),
        )
        super().__init__(name=name, **kwargs)

        self._rtp = rtp_session
        self._sample_rate = sample_rate
        self._on_dtmf_received = on_dtmf_received
        self._on_connected = on_connected
        self._cooldown_duration = input_cooldown_after_tts_secs

        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._running = False
        self._receive_task: Optional[asyncio.Task] = None
        self._connected_notified = False

        self._bot_is_speaking = False
        self._speaking_cooldown_task: Optional[asyncio.Task] = None

        # Wire RTP callbacks (called from UDP datagram loop; may be sync context).
        self._rtp.on_audio = self._on_rtp_audio
        self._rtp.on_dtmf = self._on_rtp_dtmf

    # ------------------------------------------------------------------
    # RTP callbacks
    # ------------------------------------------------------------------

    def _on_rtp_audio(self, pcm: bytes) -> None:
        """Called by RTPSession for each decoded 20 ms PCM frame."""
        if self._bot_is_speaking:
            return
        try:
            self._audio_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # drop if pipeline is slow

    def _on_rtp_dtmf(self, event: DTMFEvent) -> None:
        """Called by RTPSession on each complete DTMF digit."""
        if self._on_dtmf_received:
            asyncio.ensure_future(self._on_dtmf_received(event.digit))
        if event.digit == "#":
            asyncio.ensure_future(self.push_frame(InterruptionFrame()))

    # ------------------------------------------------------------------
    # Echo / barge-in management (mirrors AudioSocketInputTransport)
    # ------------------------------------------------------------------

    def set_bot_speaking(self, is_speaking: bool) -> None:
        if is_speaking:
            if self._speaking_cooldown_task:
                self._speaking_cooldown_task.cancel()
                self._speaking_cooldown_task = None
            self._bot_is_speaking = True
            logger.info("SIP bot speaking: input suppressed")
        else:
            if self._speaking_cooldown_task:
                self._speaking_cooldown_task.cancel()
            self._speaking_cooldown_task = asyncio.create_task(self._cooldown_timer())
            logger.info(
                f"SIP bot stopped speaking; {self._cooldown_duration}s cooldown"
            )

    def resume_input_immediately(self) -> None:
        if self._speaking_cooldown_task:
            self._speaking_cooldown_task.cancel()
            self._speaking_cooldown_task = None
        self._bot_is_speaking = False
        logger.info("SIP barge-in: input resumed")

    async def _cooldown_timer(self) -> None:
        try:
            await asyncio.sleep(self._cooldown_duration)
            self._bot_is_speaking = False
            logger.info("SIP cooldown complete: input resumed")
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, frame: StartFrame) -> None:
        result = super().start(frame)
        if asyncio.iscoroutine(result):
            await result
        self._running = True
        self._receive_task = self.create_task(
            self._receive_loop(), name="sip_input_receive"
        )
        if not self._connected_notified and self._on_connected:
            await self._on_connected()
            self._connected_notified = True
        logger.info(f"SIP input transport started @ {self._sample_rate} Hz")

    async def stop(self, frame: EndFrame) -> None:
        self._running = False
        if self._speaking_cooldown_task:
            self._speaking_cooldown_task.cancel()
            self._speaking_cooldown_task = None
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        self._drain_audio_queue()
        result = super().stop(frame)
        if asyncio.iscoroutine(result):
            await result

    async def cancel(self, frame: CancelFrame) -> None:
        self._running = False
        if self._speaking_cooldown_task:
            self._speaking_cooldown_task.cancel()
            self._speaking_cooldown_task = None
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        self._drain_audio_queue()
        result = super().cancel(frame)
        if asyncio.iscoroutine(result):
            await result

    def _drain_audio_queue(self) -> None:
        """Discard any buffered PCM bytes to release memory immediately on session end."""
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def process_frame(self, frame: Frame, direction) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self.set_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.set_bot_speaking(False)

    async def _receive_loop(self) -> None:
        try:
            while self._running:
                try:
                    pcm = await asyncio.wait_for(self._audio_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self.push_frame(
                    InputAudioRawFrame(
                        audio=pcm,
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    )
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SIP input receive loop error: {e}", exc_info=True)
            await self.push_frame(
                EndTaskFrame(reason=f"sip receive loop exception: {e}")
            )


class SIPOutputTransport(BaseOutputTransport):
    """
    Pipecat output transport for SIP/RTP calls.

    Receives OutputAudioRawFrame from the pipeline (via write_audio_frame),
    buffers audio into 20 ms chunks, and paces them to RTPSession at exactly
    20 ms intervals using a background send loop, matching the AudioSocket
    output transport pattern to avoid RTP burst flooding Zoiper's jitter buffer.
    """

    FRAME_DURATION_MS = 20

    def __init__(
        self,
        rtp_session: RTPSession,
        sample_rate: int,
        input_transport: Optional[SIPInputTransport] = None,
        name: str = "SIPOutputTransport",
        **kwargs,
    ):
        kwargs.setdefault(
            "params",
            TransportParams(
                audio_out_enabled=True,
                audio_out_sample_rate=sample_rate,
            ),
        )
        super().__init__(name=name, **kwargs)

        self._rtp = rtp_session
        self._sample_rate = sample_rate
        self._input_transport = input_transport
        self._audio_buffer = bytearray()
        self._frame_size = _frame_size_for_rate(sample_rate)

        self._resampler = None
        self._input_sample_rate = None

        self._running = False
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._send_task: Optional[asyncio.Task] = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._running = True
        self._send_task = asyncio.create_task(
            self._send_loop(), name="sip_output_send"
        )
        logger.info(f"SIP output transport started @ {self._sample_rate} Hz")

    async def stop(self, frame: EndFrame) -> None:
        self._running = False
        await self._flush_buffer()
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
            self._send_task = None
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        self._running = False
        self._audio_buffer = bytearray()
        self._clear_send_queue()
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
            self._send_task = None
        await super().cancel(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Called by Pipecat's MediaSender with TTS audio resampled to session sample rate."""
        self._handle_audio_frame(frame)
        return True

    def _handle_audio_frame(self, frame: OutputAudioRawFrame) -> None:
        """Buffer audio into 20 ms chunks, resampling if needed, then enqueue."""
        audio = frame.audio
        input_rate = frame.sample_rate

        if input_rate != self._sample_rate:
            if self._input_sample_rate != input_rate or self._resampler is None:
                self._input_sample_rate = input_rate
                self._resampler = soxr.Resample(
                    in_rate=input_rate,
                    out_rate=self._sample_rate,
                    quality=soxr.HighQuality,
                )
                logger.info(
                    f"SIPOutputTransport: resampler created {input_rate} -> {self._sample_rate} Hz"
                )
            try:
                audio_np = np.frombuffer(audio, dtype=np.int16)
                resampled_np = self._resampler.process(audio_np)
                audio = resampled_np.astype(np.int16).tobytes()
            except Exception as e:
                logger.error(f"SIPOutputTransport: resampling failed: {e}")
                return

        self._audio_buffer += audio
        while len(self._audio_buffer) >= self._frame_size:
            chunk = bytes(self._audio_buffer[: self._frame_size])
            del self._audio_buffer[: self._frame_size]
            try:
                self._send_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                logger.warning("SIP audio send queue full, dropping frame")
                break

    async def process_frame(self, frame: Frame, direction) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStoppedFrame):
            await self._flush_buffer()
        elif isinstance(frame, InterruptionFrame):
            self._audio_buffer = bytearray()
            self._clear_send_queue()
            if self._input_transport:
                self._input_transport.resume_input_immediately()

    async def _send_loop(self) -> None:
        """
        Clock-based send loop: paces 20 ms RTP frames at exact 20 ms intervals.

        Without pacing, TTS audio would be sent as a burst, overflowing Zoiper's
        jitter buffer and causing audio cuts and timing artefacts.
        """
        frame_duration = self.FRAME_DURATION_MS / 1000
        loop = asyncio.get_event_loop()
        next_send_time = loop.time()

        try:
            while self._running:
                wait_time = next_send_time - loop.time()
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                next_send_time += frame_duration

                chunk = None
                try:
                    chunk = self._send_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                if chunk is None:
                    # Send silence to keep RTP stream alive
                    if self._frame_size > 0:
                        self._rtp.send_audio(b"\x00" * self._frame_size)
                    continue

                self._rtp.send_audio(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SIP send loop error: {e}", exc_info=True)

    async def _flush_buffer(self) -> None:
        if self._audio_buffer and self._frame_size > 0:
            padding = self._frame_size - len(self._audio_buffer)
            chunk = bytes(self._audio_buffer) + b"\x00" * padding
            await self._send_queue.put(chunk)
            self._audio_buffer = bytearray()

    def _clear_send_queue(self) -> None:
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def clear_queue(self) -> None:
        """Clear buffered audio (called on barge-in)."""
        self._clear_send_queue()
        self._audio_buffer = bytearray()

    def remaining_playback_secs(self) -> float:
        """Return the estimated remaining audio playback time in seconds.

        Counts 20 ms frames still waiting in the send queue.  Called by
        TTSStateTracker when BotStoppedSpeakingFrame is received upstream,
        at which point all TTS audio has been written to the queue.
        """
        return self._send_queue.qsize() * (self.FRAME_DURATION_MS / 1000.0)


class SIPTransport(BaseTransport):
    """
    Combined SIP/RTP Pipecat transport.

    Mirrors AudioSocketTransport but wraps RTPSession instead of
    AudioSocketProtocol.  Instantiate one per call and pass to AgentFactory.
    """

    def __init__(
        self,
        rtp_session: RTPSession,
        sample_rate: int = 8000,
        input_cooldown_after_tts_secs: float = 0.5,
        on_dtmf_received: Optional[Callable[[str], Awaitable[None]]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._rtp = rtp_session
        self._sample_rate = sample_rate
        self._cooldown = input_cooldown_after_tts_secs
        self._on_dtmf_received = on_dtmf_received

        self._input: Optional[SIPInputTransport] = None
        self._output: Optional[SIPOutputTransport] = None
        self._client_connected_emitted = False
        self._client_disconnected_emitted = False

        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    async def _emit_client_connected(self) -> None:
        if self._client_connected_emitted:
            return
        self._client_connected_emitted = True
        await self._call_event_handler("on_client_connected", None)

    async def _emit_client_disconnected(self) -> None:
        if self._client_disconnected_emitted:
            return
        self._client_disconnected_emitted = True
        await self._call_event_handler("on_client_disconnected", None)

    def input(self) -> SIPInputTransport:
        if self._input is None:
            self._input = SIPInputTransport(
                rtp_session=self._rtp,
                sample_rate=self._sample_rate,
                on_dtmf_received=self._on_dtmf_received,
                on_connected=self._emit_client_connected,
                input_cooldown_after_tts_secs=self._cooldown,
                name="SIPInputTransport",
            )
        return self._input

    def output(self) -> SIPOutputTransport:
        if self._output is None:
            logger.warning("SIPTransport.output() creating new SIPOutputTransport")
            self._output = SIPOutputTransport(
                rtp_session=self._rtp,
                sample_rate=self._sample_rate,
                input_transport=self._input,
                name="SIPOutputTransport",
            )
        return self._output

    async def close(self) -> None:
        logger.info("Closing SIP transport")
        await self._emit_client_disconnected()
        # Clear RTP callbacks before stopping to break the reference cycle:
        #   RTPSession.on_audio -> SIPInputTransport._on_rtp_audio -> SIPInputTransport
        #   SIPInputTransport._rtp -> RTPSession
        # Without this, Python's refcount-based GC cannot free either object;
        # only the cyclic GC can, and it only runs when sessions are idle.
        self._rtp.on_audio = None
        self._rtp.on_dtmf = None
        await self._rtp.stop()
        # Drop references to input/output transports so their audio queues,
        # send queues, and any remaining closures are freed promptly.
        self._input = None
        self._output = None

    def on_interruption(self) -> None:
        if self._output:
            self._output.clear_queue()
        if self._input:
            self._input.resume_input_immediately()
