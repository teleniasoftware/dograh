"""
SIP dialog state machine.

Handles inbound call setup (INVITE -> 100/200 -> ACK -> ESTABLISHED),
teardown (BYE), cancellation (CANCEL), and blind transfer (REFER).
"""

import asyncio
import logging
import random
import string
from enum import Enum, auto
from typing import Awaitable, Callable, Optional

from .call_info import SIPCallInfo
from .message import (
    _STATUS_REASONS,
    build_request,
    build_response,
    extract_tag,
    extract_uri,
    extract_user,
)
from .rtp_session import RTPPortAllocator, RTPSession
from .sdp import CODEC_PARAMS
from .sdp import build as build_sdp
from .sdp import find_dtmf_pt, select_codec
from .sdp import parse as parse_sdp

logger = logging.getLogger(__name__)


def _rand_tag(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class DialogState(Enum):
    IDLE = auto()
    RINGING = auto()  # 100/200 sent, waiting for ACK
    ESTABLISHED = auto()  # ACK received, media flowing
    TERMINATED = auto()


class SIPDialog:
    """
    State machine for a single SIP dialog (inbound call).

    The SIPServer creates one SIPDialog per Call-ID and feeds it raw SIP
    messages.  The dialog calls back when established or terminated.

    Callbacks:
        on_established(SIPCallInfo): called after ACK, media flowing
        on_terminated():             called when dialog ends for any reason
    """

    # RFC 3261 retransmission timers (seconds)
    T1 = 0.5
    T2 = 4.0
    # Timeout for no ACK (RFC 3261 suggests 64s, we use 30s to clean up faster)
    ACK_TIMEOUT = 30

    def __init__(
        self,
        call_id: str,
        local_ip: str,
        local_sip_port: int,
        port_allocator: RTPPortAllocator,
        session_id: int,
        on_established: Callable[[SIPCallInfo], Awaitable[None]],
        on_terminated: Callable[[], Awaitable[None]],
        send_fn: Callable[[bytes, tuple], Awaitable[None]],
        on_pre_call: Optional[
            Callable[[str, str, dict[str, str]], Awaitable[Optional[int]]]
        ] = None,
    ):
        self.call_id = call_id
        self._local_ip = local_ip
        self._local_sip_port = local_sip_port
        self._port_allocator = port_allocator
        self._session_id = session_id
        self._on_established = on_established
        self._on_terminated = on_terminated
        self._send = send_fn
        self._on_pre_call = on_pre_call

        self.state = DialogState.IDLE

        # Populated on INVITE
        self._invite: Optional[object] = None  # SIPMessage
        self._remote_addr: Optional[tuple] = None
        self._local_tag: str = _rand_tag()
        self._cseq: int = 1

        self._rtp_session: Optional[RTPSession] = None
        self._rtp_local_port: int = 0
        self._call_info: Optional[SIPCallInfo] = None

        # 200 OK retransmission
        self._200ok_bytes: Optional[bytes] = None
        self._retransmit_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def rtp_session(self) -> Optional[RTPSession]:
        return self._rtp_session

    @property
    def call_info(self) -> Optional[SIPCallInfo]:
        return self._call_info

    async def handle_message(self, msg, addr: tuple) -> None:
        """Dispatch an inbound SIP message to the appropriate handler."""
        if msg.is_request:
            logger.debug(f"[{self.call_id}] {self.state.name} <- {msg.method}")
            if msg.method == "INVITE":
                await self._on_invite(msg, addr)
            elif msg.method == "ACK":
                await self._on_ack(msg, addr)
            elif msg.method == "BYE":
                await self._on_bye(msg, addr)
            elif msg.method == "CANCEL":
                await self._on_cancel(msg, addr)
            elif msg.method == "REFER":
                await self._on_refer(msg, addr)
        else:
            logger.debug(f"[{self.call_id}] {self.state.name} <- {msg.status_code}")

    async def hangup(
        self,
        extra_headers: Optional[dict] = None,
        reason_code: Optional[int] = None,
    ) -> None:
        """Terminate the dialog by sending BYE (if established).

        Args:
            extra_headers: Optional extra SIP headers to include in the BYE request
                           (e.g. ``{"X-Telenia-Transcript": "testo trascritto"}``).
            reason_code: Optional SIP status code added as a ``Reason`` header in the
                         BYE so the remote party knows why the call was terminated
                         (e.g. 402 for insufficient credits, 486 for service unavailable).
        """
        if reason_code is not None:
            extra_headers = dict(extra_headers or {})
            reason_text = _STATUS_REASONS.get(reason_code, "Error")
            extra_headers["Reason"] = f'SIP;cause={reason_code};text="{reason_text}"'
        if self.state == DialogState.ESTABLISHED:
            await self._send_bye(extra_headers=extra_headers)
        await self._terminate()

    async def reject(self, status_code: int) -> None:
        """Reject the incoming call with a SIP error response.

        Must be called while the dialog is in RINGING state (after 100 Trying,
        before 200 OK).  Sends *status_code* as the final response to the INVITE
        and terminates the dialog without allocating any media resources.

        Args:
            status_code: SIP error code to send (e.g. 402, 486, 503).
        """
        if self.state != DialogState.RINGING or not self._invite or not self._remote_addr:
            logger.warning(
                f"[{self.call_id}] reject({status_code}) called in invalid state "
                f"{self.state.name}; ignoring"
            )
            return
        resp = build_response(self._invite, status_code, local_tag=self._local_tag)
        await self._send(resp, self._remote_addr)
        logger.info(f"[{self.call_id}] Call rejected: {status_code}")
        await self._terminate()

    async def blind_transfer(self, target_uri: str) -> None:
        """
        Initiate blind transfer by sending REFER to the caller.

        Args:
            target_uri: SIP URI to transfer the caller to (e.g. sip:+39123@host)
        """
        if (
            self.state != DialogState.ESTABLISHED
            or not self._invite
            or not self._remote_addr
        ):
            logger.warning(f"[{self.call_id}] blind_transfer: not in ESTABLISHED state")
            return

        via = (
            f"SIP/2.0/UDP {self._local_ip}:{self._local_sip_port}"
            f";branch=z9hG4bK{_rand_tag(12)}"
        )
        refer_bytes = build_request(
            method="REFER",
            request_uri=extract_uri(self._invite.from_header),
            from_tag=self._local_tag,
            to_tag=extract_tag(self._invite.from_header),
            from_uri=f"sip:voicebot@{self._local_ip}",
            to_uri=self._invite.from_header,
            call_id=self.call_id,
            cseq=self._cseq + 2,
            via=via,
            contact=f"<sip:voicebot@{self._local_ip}:{self._local_sip_port}>",
            extra={
                "Refer-To": target_uri,
                "Referred-By": f"<sip:voicebot@{self._local_ip}>",
            },
        )
        await self._send(refer_bytes, self._remote_addr)
        logger.info(f"[{self.call_id}] REFER sent to {target_uri}")

    # ------------------------------------------------------------------
    # Inbound request handlers
    # ------------------------------------------------------------------

    async def _on_invite(self, msg, addr: tuple) -> None:
        if self.state not in (DialogState.IDLE, DialogState.RINGING):
            # Re-INVITE not supported
            resp = build_response(msg, 488, local_tag=self._local_tag)
            await self._send(resp, addr)
            return

        self._invite = msg
        self._remote_addr = addr

        from_hdr = getattr(msg, "from_header", "") or ""
        to_hdr = getattr(msg, "to_header", "") or ""
        logger.info(
            f"[{self.call_id}] INVITE from={from_hdr!r} to={to_hdr!r} addr={addr}"
        )

        # 100 Trying
        trying = build_response(msg, 100)
        await self._send(trying, addr)
        logger.info(f"[{self.call_id}] 100 Trying to {addr}")
        self.state = DialogState.RINGING

        # Pre-answer check: circuit breaker / credits / quota.
        # Returns a SIP error code to reject with, or None to proceed.
        if self._on_pre_call is not None:
            callee = extract_user(extract_uri(msg.to_header))
            rejection_code = await self._on_pre_call(
                callee,
                self.call_id,
                dict(msg.headers),
            )
            if rejection_code is not None:
                await self.reject(rejection_code)
                return

        # Parse remote SDP
        remote_sdp = parse_sdp(msg.body)
        codec_pt, codec_name = select_codec(remote_sdp)
        dtmf_pt = find_dtmf_pt(remote_sdp) or 101
        # Audio sample rate depends on negotiated codec (G722 -> 16 kHz, G.711 -> 8 kHz)
        sample_rate = CODEC_PARAMS.get(codec_name.upper(), (8000, 8000))[0]

        rtp_ip = (
            (remote_sdp.audio.connection_ip if remote_sdp.audio else "")
            or remote_sdp.connection_ip
            or addr[0]
        )
        rtp_port = remote_sdp.audio.port if remote_sdp.audio else 0

        # Allocate RTP port and start session
        self._rtp_local_port = self._port_allocator.allocate()
        self._rtp_session = RTPSession(
            local_ip=self._local_ip,
            local_port=self._rtp_local_port,
            remote_addr=(rtp_ip, rtp_port),
            codec=codec_name,
            payload_type=codec_pt,
            dtmf_payload_type=dtmf_pt,
        )
        await self._rtp_session.start()
        logger.info(
            f"[{self.call_id}] RTP session started: "
            f"local={self._local_ip}:{self._rtp_local_port} "
            f"remote={rtp_ip}:{rtp_port} codec={codec_name}"
        )

        # Build SDP answer
        local_sdp = build_sdp(
            local_ip=self._local_ip,
            rtp_port=self._rtp_local_port,
            codec_pt=codec_pt,
            codec_name=codec_name,
            sample_rate=sample_rate,
            dtmf_pt=dtmf_pt,
            session_id=str(self._session_id),
        )

        # Collect call info from SIP headers
        from_uri = extract_uri(msg.from_header)
        to_uri = extract_uri(msg.to_header)
        caller_number = extract_user(from_uri)
        callee_number = extract_user(to_uri)

        self._call_info = SIPCallInfo(
            session_id=self._session_id,
            call_uuid=self.call_id,
            sip_call_id=self.call_id,
            from_uri=from_uri,
            to_uri=to_uri,
            caller_number=caller_number,
            callee_number=callee_number,
            agent_id=callee_number,
            codec=codec_name,
            sample_rate=sample_rate,
            rtp_local_port=self._rtp_local_port,
            rtp_remote_addr=(rtp_ip, rtp_port),
            dtmf_payload_type=dtmf_pt,
            all_headers=dict(msg.headers),
        )

        # 200 OK with SDP
        self._200ok_bytes = build_response(
            msg,
            200,
            local_tag=self._local_tag,
            body=local_sdp,
            extra={
                "Contact": f"<sip:voicebot@{self._local_ip}:{self._local_sip_port}>"
            },
        )
        await self._send(self._200ok_bytes, addr)
        logger.info(
            f"[{self.call_id}] 200 OK sent: {codec_name}/{sample_rate} "
            f"RTP={self._local_ip}:{self._rtp_local_port} remote={rtp_ip}:{rtp_port}"
        )

        # Retransmit 200 OK until ACK
        self._retransmit_task = asyncio.create_task(
            self._retransmit_200ok(addr), name=f"retransmit_{self.call_id[:8]}"
        )

    async def _on_ack(self, msg, addr: tuple) -> None:
        if self.state != DialogState.RINGING:
            return

        if self._retransmit_task:
            self._retransmit_task.cancel()
            self._retransmit_task = None

        self.state = DialogState.ESTABLISHED
        logger.info(f"[{self.call_id}] ESTABLISHED (ACK from {addr})")

        if self._on_established and self._call_info:
            await self._on_established(self._call_info)

    async def _on_bye(self, msg, addr: tuple) -> None:
        resp = build_response(msg, 200, local_tag=self._local_tag)
        await self._send(resp, addr)
        logger.info(f"[{self.call_id}] BYE received; terminating")
        await self._terminate()

    async def _on_cancel(self, msg, addr: tuple) -> None:
        resp = build_response(msg, 200, local_tag=self._local_tag)
        await self._send(resp, addr)
        if self._invite:
            resp487 = build_response(self._invite, 487, local_tag=self._local_tag)
            await self._send(resp487, addr)
        logger.info(f"[{self.call_id}] CANCEL received; terminating")
        await self._terminate()

    async def _on_refer(self, msg, addr: tuple) -> None:
        # Inbound REFER (transfer to us) is not supported
        resp = build_response(msg, 603, local_tag=self._local_tag)
        await self._send(resp, addr)

    # ------------------------------------------------------------------
    # Outbound BYE
    # ------------------------------------------------------------------

    async def _send_bye(self, extra_headers: Optional[dict] = None) -> None:
        if not self._invite or not self._remote_addr:
            return
        via = (
            f"SIP/2.0/UDP {self._local_ip}:{self._local_sip_port}"
            f";branch=z9hG4bK{_rand_tag(12)}"
        )
        bye_bytes = build_request(
            method="BYE",
            request_uri=extract_uri(self._invite.from_header),
            from_tag=self._local_tag,
            to_tag=extract_tag(self._invite.from_header),
            from_uri=f"sip:voicebot@{self._local_ip}",
            to_uri=self._invite.from_header,
            call_id=self.call_id,
            cseq=self._cseq + 1,
            via=via,
            contact=f"<sip:voicebot@{self._local_ip}:{self._local_sip_port}>",
            extra=extra_headers,
        )
        await self._send(bye_bytes, self._remote_addr)

    # ------------------------------------------------------------------
    # 200 OK retransmission (RFC 3261 section 13.3.1.4)
    # ------------------------------------------------------------------

    async def _retransmit_200ok(self, addr: tuple) -> None:
        interval = self.T1
        elapsed = 0.0
        try:
            while True:
                await asyncio.sleep(interval)
                elapsed += interval
                if self.state != DialogState.RINGING:
                    return
                # Timeout: hangup if no ACK received after ACK_TIMEOUT seconds
                if elapsed >= self.ACK_TIMEOUT:
                    logger.warning(
                        f"[{self.call_id}] ACK timeout ({self.ACK_TIMEOUT}s), forcing hangup"
                    )
                    await self.hangup()
                    return
                logger.debug(
                    f"[{self.call_id}] Retransmitting 200 OK (interval={interval:.1f}s)"
                )
                await self._send(self._200ok_bytes, addr)
                interval = min(interval * 2, self.T2)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _terminate(self) -> None:
        if self.state == DialogState.TERMINATED:
            return
        self.state = DialogState.TERMINATED

        if self._retransmit_task:
            self._retransmit_task.cancel()
            self._retransmit_task = None

        if self._rtp_session:
            await self._rtp_session.stop()
            self._rtp_session = None

        if self._on_terminated:
            await self._on_terminated()
