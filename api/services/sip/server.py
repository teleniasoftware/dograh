"""
Minimal asyncio SIP UDP server.

Listens on a UDP port, demultiplexes inbound SIP messages by Call-ID to
SIPDialog instances, and notifies the application when calls are established
or terminated.
"""

import asyncio
import hashlib
import logging
import re
import secrets
import socket
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_MAX_KNOWN_PEERS = 1000

from .call_info import SIPCallInfo
from .dialog import SIPDialog
from .message import build_response, parse_message
from .rtp_session import RTPPortAllocator


class _SIPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "SIPServer"):
        self._server = server

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        asyncio.ensure_future(self._server._on_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        logger.debug(f"SIP UDP socket error: {exc}")

    def connection_lost(self, exc) -> None:
        pass


class SIPServer:
    """
    UDP SIP server.

    Accepts inbound SIP calls, manages one SIPDialog per Call-ID, and
    notifies the application layer via async callbacks.

    Callbacks:
        on_call_established(SIPCallInfo): new call is ready for media
        on_call_terminated(call_id):      dialog ended (BYE/CANCEL/error)
    """

    _AUTH_REALM = "flexi"
    _NONCE_TTL = 300  # seconds a challenge nonce stays valid

    def __init__(
        self,
        host: str,
        port: int,
        on_call_established: Callable[[SIPCallInfo], Awaitable[None]],
        on_call_terminated: Callable[[str], Awaitable[None]],
        rtp_start_port: int = 10000,
        rtp_end_port: int = 20000,
        on_pre_call: Optional[
            Callable[[str, str, dict[str, str]], Awaitable[Optional[int]]]
        ] = None,
        auth_username: str = "",
        auth_password: str = "",
    ):
        self._host = host
        self._port = port
        self._on_call_established = on_call_established
        self._on_call_terminated = on_call_terminated
        self._on_pre_call = on_pre_call
        self._port_allocator = RTPPortAllocator(rtp_start_port, rtp_end_port)
        self._auth_username = auth_username
        self._auth_password = auth_password

        self._dialogs: dict[str, SIPDialog] = {}
        self._call_counter: int = 0
        self._transport: Optional[asyncio.DatagramTransport] = None
        # IPs that have sent at least one INVITE; trusted for OPTIONS keepalives.
        # OrderedDict used for LRU eviction when the peer set reaches its max size.
        self._known_peers: OrderedDict[str, None] = OrderedDict()
        # call_id -> (nonce, expire_time): pending digest challenges
        self._pending_nonces: dict[str, tuple[str, float]] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SIPProtocol(self),
            local_addr=(self._host, self._port),
            reuse_port=True,
        )
        logger.info(f"SIP server listening on {self._host}:{self._port} (UDP)")

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        for dialog in list(self._dialogs.values()):
            await dialog.hangup()
        self._dialogs.clear()
        logger.info("SIP server stopped")

    def get_dialog(self, call_id: str) -> Optional[SIPDialog]:
        return self._dialogs.get(call_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _on_datagram(self, data: bytes, addr: tuple) -> None:
        msg = parse_message(data)
        if msg is None:
            logger.debug(f"Failed to parse SIP message from {addr}")
            return

        call_id = msg.call_id
        if not call_id:
            logger.warning(f"SIP message without Call-ID from {addr}")
            return

        # Handle OPTIONS statelessly: respond 200 OK to known peers, drop silently otherwise.
        if msg.is_request and msg.method == "OPTIONS":
            if addr[0] in self._known_peers:
                await self._send(build_response(msg, 200), addr)
            return

        if msg.is_request and msg.method == "INVITE" and call_id not in self._dialogs:
            # Digest authentication challenge (only when credentials are configured)
            if self._auth_username:
                auth_hdr = msg.headers.get("authorization", "")
                if not auth_hdr:
                    self._challenge_invite(msg, addr, call_id)
                    return
                if not self._verify_digest(
                    "INVITE",
                    auth_hdr,
                    call_id,
                    msg.request_uri or "",
                ):
                    logger.warning(f"[{call_id}] SIP auth failed from {addr}")
                    resp = build_response(msg, 403)
                    asyncio.ensure_future(self._send(resp, addr))
                    return
                self._pending_nonces.pop(call_id, None)
                logger.debug(f"[{call_id}] SIP auth OK from {addr}")

            # LRU eviction: touch existing peer or add new one
            if addr[0] in self._known_peers:
                self._known_peers.move_to_end(addr[0])
            else:
                if len(self._known_peers) >= _MAX_KNOWN_PEERS:
                    self._known_peers.popitem(last=False)  # evict oldest
                self._known_peers[addr[0]] = None
            self._call_counter += 1
            session_id = self._call_counter
            local_ip = self._resolve_local_ip(addr)

            async def _on_established(info: SIPCallInfo) -> None:
                await self._on_call_established(info)

            async def _on_terminated() -> None:
                self._dialogs.pop(call_id, None)
                await self._on_call_terminated(call_id)

            dialog = SIPDialog(
                call_id=call_id,
                local_ip=local_ip,
                local_sip_port=self._port,
                port_allocator=self._port_allocator,
                session_id=session_id,
                on_established=_on_established,
                on_terminated=_on_terminated,
                send_fn=self._send,
                on_pre_call=self._on_pre_call,
            )
            self._dialogs[call_id] = dialog
            logger.info(f"New inbound call #{session_id} Call-ID={call_id} from {addr}")

        dialog = self._dialogs.get(call_id)
        if dialog is None:
            if msg.is_request:
                msg_desc = msg.method
            else:
                msg_desc = f"{msg.status_code} {msg.reason}"
            logger.debug(
                f"No dialog for Call-ID={call_id} [{msg_desc}] from {addr}, ignoring"
            )
            return

        await dialog.handle_message(msg, addr)

    def _challenge_invite(self, msg, addr: tuple, call_id: str) -> None:
        """Send 401 Unauthorized with a fresh digest challenge nonce."""
        now = time.time()
        # Evict expired nonces opportunistically
        expired = [k for k, (_, exp) in self._pending_nonces.items() if now > exp]
        for k in expired:
            del self._pending_nonces[k]

        nonce = secrets.token_hex(16)
        self._pending_nonces[call_id] = (nonce, now + self._NONCE_TTL)
        www_auth = (
            f'Digest realm="{self._AUTH_REALM}",'
            f'nonce="{nonce}",algorithm=MD5,qop="auth"'
        )
        resp = build_response(msg, 401, extra={"WWW-Authenticate": www_auth})
        asyncio.ensure_future(self._send(resp, addr))
        logger.debug(f"[{call_id}] SIP auth challenge sent to {addr}")

    def _verify_digest(
        self, method: str, auth_header: str, call_id: str, request_uri: str
    ) -> bool:
        """Verify an incoming Authorization header against stored nonce + credentials."""
        params: dict[str, str] = {}
        for m in re.finditer(r'(\w+)=["\']?([^"\',\s>]+)["\']?', auth_header):
            params[m.group(1)] = m.group(2)

        username = params.get("username", "")
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        uri = params.get("uri", request_uri)
        response = params.get("response", "")
        cnonce = params.get("cnonce", "")
        nc = params.get("nc", "00000001")
        qop = params.get("qop", "auth")

        if username != self._auth_username:
            return False

        stored = self._pending_nonces.get(call_id)
        if not stored:
            return False
        stored_nonce, expire = stored
        if time.time() > expire or nonce != stored_nonce:
            return False

        ha1 = hashlib.md5(
            f"{username}:{realm}:{self._auth_password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop in ("auth", "auth-int"):
            expected = hashlib.md5(
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()

        return secrets.compare_digest(response, expected)

    async def _send(self, data: bytes, addr: tuple) -> None:
        if self._transport:
            try:
                self._transport.sendto(data, addr)
            except Exception as e:
                logger.warning(f"SIP send error to {addr}: {e}")

    def _resolve_local_ip(self, remote_addr: tuple) -> str:
        """Return the local IP that would be used to reach remote_addr."""
        if self._host not in ("0.0.0.0", ""):
            return self._host
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((remote_addr[0], 1))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
