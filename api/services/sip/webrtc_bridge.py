"""Browser WebRTC to local SIP/RTP bridge used by SIP audio tests."""

from __future__ import annotations

import asyncio
import os
import random
import re
import socket
import uuid
from fractions import Fraction
from typing import Awaitable, Callable, Optional

import numpy as np
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from av import AudioFrame
from av.audio.resampler import AudioResampler
from loguru import logger

from api.services.sip.message import (
    build_request,
    extract_tag,
    extract_uri,
    parse_message,
)
from api.services.sip.rtp_session import RTPSession
from api.services.sip.sdp import build as build_sdp
from api.services.sip.sdp import find_dtmf_pt, parse as parse_sdp, select_codec
from api.services.sip.test_registry import (
    register_sip_test_session,
    unregister_sip_test_sender,
)

SIP_TEST_SESSION_HEADER = "X-Dograh-Sip-Test-Session"

_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_RESERVED_HEADERS = {
    "via",
    "from",
    "to",
    "call-id",
    "cseq",
    "contact",
    "max-forwards",
    "user-agent",
    "allow",
    "content-type",
    "content-length",
}


class QueuedAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, sample_rate: int) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._pts = 0

    def push_pcm(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        super().stop()

    async def recv(self) -> AudioFrame:
        pcm = await self._queue.get()
        if pcm is None:
            raise asyncio.CancelledError

        samples = np.frombuffer(pcm, dtype=np.int16)
        frame = AudioFrame.from_ndarray(
            samples.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        frame.sample_rate = self._sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self._sample_rate)
        self._pts += samples.size
        return frame


class _SIPClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "SIPTestClient") -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        asyncio.create_task(self._client.handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        logger.debug(f"SIP test client UDP error: {exc}")


class SIPTestClient:
    """Small SIP UAC that calls the local Dograh SIP ingress."""

    def __init__(
        self,
        *,
        workflow_uuid: str,
        test_session_id: str,
        custom_headers: dict[str, str],
        on_audio: Callable[[bytes], None],
    ) -> None:
        self.workflow_uuid = workflow_uuid
        self.test_session_id = test_session_id
        self.custom_headers = custom_headers
        self.on_audio = on_audio

        self.host = os.getenv("SIP_TEST_TARGET_HOST", "127.0.0.1")
        self.port = int(os.getenv("SIP_TEST_TARGET_PORT", os.getenv("SIP_PORT", "5090")))
        self.local_ip = os.getenv("SIP_TEST_LOCAL_IP", "127.0.0.1")
        self.call_id = f"{uuid.uuid4()}@dograh-sip-test"
        self.from_tag = uuid.uuid4().hex[:10]
        self.to_tag = ""
        self.cseq = random.randint(1000, 9999)
        self.rtp_sample_rate = 8000

        self._transport: Optional[asyncio.DatagramTransport] = None
        self._rtp: Optional[RTPSession] = None
        self._local_sip_port = 0
        self._local_rtp_port = 0
        self._invite_response: asyncio.Future[tuple[int, object]] | None = None
        self._closed = False

    @property
    def sample_rate(self) -> int:
        return self.rtp_sample_rate

    async def start(self) -> None:
        self._local_rtp_port = _pick_udp_port(self.local_ip)
        loop = asyncio.get_running_loop()
        self._invite_response = loop.create_future()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SIPClientProtocol(self),
            local_addr=(self.local_ip, 0),
        )
        sock = self._transport.get_extra_info("socket")
        self._local_sip_port = int(sock.getsockname()[1])

        await self._send_invite()
        status, response = await asyncio.wait_for(self._invite_response, timeout=15)
        if status != 200:
            raise RuntimeError(f"SIP server rejected test call with status {status}")

        remote_sdp = parse_sdp(response.body)
        codec_pt, codec_name = select_codec(remote_sdp)
        self.rtp_sample_rate = 16000 if codec_name.upper() in {"G722", "OPUS"} else 8000
        rtp_ip = (
            (remote_sdp.audio.connection_ip if remote_sdp.audio else "")
            or remote_sdp.connection_ip
            or self.host
        )
        rtp_port = remote_sdp.audio.port if remote_sdp.audio else 0
        dtmf_pt = find_dtmf_pt(remote_sdp) or 101

        self._rtp = RTPSession(
            local_ip=self.local_ip,
            local_port=self._local_rtp_port,
            remote_addr=(rtp_ip, rtp_port),
            codec=codec_name,
            payload_type=codec_pt,
            dtmf_payload_type=dtmf_pt,
            on_audio=self.on_audio,
        )
        await self._rtp.start()
        await self._send_ack(response)

    async def handle_datagram(self, data: bytes, addr: tuple) -> None:
        msg = parse_message(data)
        if msg is None or msg.is_request or msg.call_id != self.call_id:
            return
        if msg.status_code == 100:
            return
        if msg.status_code and msg.status_code >= 200 and self._invite_response:
            if not self._invite_response.done():
                self._invite_response.set_result((msg.status_code, msg))

    def send_audio(self, pcm: bytes) -> None:
        if self._rtp:
            self._rtp.send_audio(pcm)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_bye()
        finally:
            if self._rtp:
                await self._rtp.stop()
                self._rtp = None
            if self._transport:
                self._transport.close()
                self._transport = None

    async def _send_invite(self) -> None:
        body = build_sdp(
            local_ip=self.local_ip,
            rtp_port=self._local_rtp_port,
            codec_pt=8,
            codec_name="PCMA",
            sample_rate=8000,
            session_id=str(random.randint(100000, 999999)),
        )
        extra = {
            SIP_TEST_SESSION_HEADER: self.test_session_id,
            **self.custom_headers,
        }
        request = build_request(
            method="INVITE",
            request_uri=f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            from_tag=self.from_tag,
            to_tag="",
            from_uri=f"sip:browser-test@{self.local_ip}",
            to_uri=f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            call_id=self.call_id,
            cseq=self.cseq,
            via=(
                f"SIP/2.0/UDP {self.local_ip}:{self._local_sip_port}"
                f";branch=z9hG4bK{uuid.uuid4().hex[:12]}"
            ),
            contact=f"<sip:browser-test@{self.local_ip}:{self._local_sip_port}>",
            body=body,
            extra=extra,
        )
        self._send(request)

    async def _send_ack(self, response) -> None:
        self.to_tag = extract_tag(response.to_header)
        request = build_request(
            method="ACK",
            request_uri=extract_uri(response.contact)
            or f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            from_tag=self.from_tag,
            to_tag=self.to_tag,
            from_uri=f"sip:browser-test@{self.local_ip}",
            to_uri=f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            call_id=self.call_id,
            cseq=self.cseq,
            via=(
                f"SIP/2.0/UDP {self.local_ip}:{self._local_sip_port}"
                f";branch=z9hG4bK{uuid.uuid4().hex[:12]}"
            ),
            contact=f"<sip:browser-test@{self.local_ip}:{self._local_sip_port}>",
        )
        self._send(request)

    async def _send_bye(self) -> None:
        if not self._transport or not self.to_tag:
            return
        self.cseq += 1
        request = build_request(
            method="BYE",
            request_uri=f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            from_tag=self.from_tag,
            to_tag=self.to_tag,
            from_uri=f"sip:browser-test@{self.local_ip}",
            to_uri=f"sip:{self.workflow_uuid}@{self.host}:{self.port}",
            call_id=self.call_id,
            cseq=self.cseq,
            via=(
                f"SIP/2.0/UDP {self.local_ip}:{self._local_sip_port}"
                f";branch=z9hG4bK{uuid.uuid4().hex[:12]}"
            ),
            contact=f"<sip:browser-test@{self.local_ip}:{self._local_sip_port}>",
        )
        self._send(request)

    def _send(self, data: bytes) -> None:
        if self._transport:
            self._transport.sendto(data, (self.host, self.port))


class SIPWebRTCTestBridge:
    def __init__(
        self,
        *,
        workflow_id: int,
        workflow_uuid: str,
        organization_id: int,
        user_id: int,
        test_session_id: str,
        custom_headers: dict[str, str],
        sender: Callable[[dict], Awaitable[None]],
        ice_servers: list[RTCIceServer] | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.workflow_uuid = workflow_uuid
        self.organization_id = organization_id
        self.user_id = user_id
        self.test_session_id = test_session_id
        self.sender = sender
        self.ice_servers = ice_servers or []
        self.output_track = QueuedAudioTrack(sample_rate=8000)
        self.pc: Optional[RTCPeerConnection] = None
        self.sip_client = SIPTestClient(
            workflow_uuid=workflow_uuid,
            test_session_id=test_session_id,
            custom_headers=custom_headers,
            on_audio=self.output_track.push_pcm,
        )
        self._audio_task: Optional[asyncio.Task] = None

    async def handle_offer(self, payload: dict) -> dict:
        register_sip_test_session(
            self.test_session_id,
            workflow_id=self.workflow_id,
            workflow_uuid=self.workflow_uuid,
            organization_id=self.organization_id,
            user_id=self.user_id,
            sender=self.sender,
        )

        await self.sip_client.start()
        self.output_track._sample_rate = self.sip_client.sample_rate

        self.pc = RTCPeerConnection(
            RTCConfiguration(iceServers=self.ice_servers)
        )
        self.pc.addTrack(self.output_track)

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self._audio_task = asyncio.create_task(
                    self._pump_webrtc_audio(track),
                    name=f"sip_test_audio_{self.test_session_id[:8]}",
                )

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=payload.get("sdp", ""), type=payload.get("type", "offer"))
        )
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return {
            "sdp": self.pc.localDescription.sdp,
            "type": self.pc.localDescription.type,
            "pc_id": payload.get("pc_id"),
        }

    async def add_ice_candidate(self, candidate) -> None:
        if self.pc:
            await self.pc.addIceCandidate(candidate)

    async def close(self) -> None:
        unregister_sip_test_sender(self.test_session_id)
        if self._audio_task:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except asyncio.CancelledError:
                pass
            self._audio_task = None
        await self.sip_client.close()
        self.output_track.stop()
        if self.pc:
            await self.pc.close()
            self.pc = None

    async def _pump_webrtc_audio(self, track) -> None:
        resampler = AudioResampler(
            format="s16",
            layout="mono",
            rate=self.sip_client.sample_rate,
        )
        try:
            while True:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    self.sip_client.send_audio(converted.to_ndarray().tobytes())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"SIP test WebRTC audio pump stopped: {exc}")


def normalize_custom_headers(raw_headers: object) -> dict[str, str]:
    if not isinstance(raw_headers, list):
        return {}

    headers: dict[str, str] = {}
    for item in raw_headers[:20]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        if not key and not value:
            continue
        lower_key = key.lower()
        if not key or not _HEADER_RE.match(key):
            raise ValueError(f"Invalid SIP header name: {key or '(empty)'}")
        if lower_key in _RESERVED_HEADERS or lower_key == SIP_TEST_SESSION_HEADER.lower():
            raise ValueError(f"SIP header is reserved: {key}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"Invalid SIP header value for {key}")
        headers[key] = value
    return headers


def _pick_udp_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
