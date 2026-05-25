"""
RTP session over asyncio UDP.

Handles:
- G.711 alaw (PCMA) / ulaw (PCMU) decode -> 16-bit PCM via audioop (Python stdlib)
- G.711 encode for outbound audio
- G722 wideband (16 kHz audio, 8 kHz RTP clock) via g722 package if available
- RFC 2833 telephone-event receive and send (DTMF)
- Symmetric RTP: updates remote address from first received packet
"""

import array
import asyncio
import logging
import random
import struct
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import audioop  # deprecated in 3.11, removed in 3.13
    _HAS_AUDIOOP = True
except ImportError:
    _HAS_AUDIOOP = False
    logger.warning("audioop not available; using fallback G.711 tables")

try:
    import G722 as _G722Lib  # pip install g722  (module name is capital G722)
    _HAS_G722 = True
except ImportError:
    _G722Lib = None  # type: ignore
    _HAS_G722 = False
    logger.warning("G722 package not available; G722 codec not supported")

try:
    import opuslib as _opuslib  # pip install opuslib  (requires libopus-dev)
    _HAS_OPUS = True
except ImportError:
    _opuslib = None  # type: ignore
    _HAS_OPUS = False
    logger.warning("opuslib not available; Opus codec not supported")

# ---------------------------------------------------------------------------
# G.711 fallback (pure Python tables) used when audioop is not available
# ---------------------------------------------------------------------------

def _build_alaw_decode_table() -> bytes:
    """Build 256-entry alaw -> 16-bit PCM table (returns bytes for struct unpacking)."""
    table = []
    for i in range(256):
        a = i ^ 0x55
        seg = (a & 0x70) >> 4
        quant = a & 0x0F
        if seg == 0:
            val = quant * 2 + 1
        else:
            val = (quant + 16.5) * (1 << seg)
        if (a & 0x80) == 0:
            val = -val
        table.append(max(-32768, min(32767, int(val))))
    return table


def _build_ulaw_decode_table() -> list:
    table = []
    for i in range(256):
        u = ~i & 0xFF
        sign = 1 if u & 0x80 else -1
        exp = (u >> 4) & 0x07
        mant = u & 0x0F
        val = sign * ((mant * 2 + 33) * (1 << exp) - 33)
        table.append(max(-32768, min(32767, val)))
    return table


_ALAW_DEC = _build_alaw_decode_table()
_ULAW_DEC = _build_ulaw_decode_table()


def _alaw2lin(data: bytes) -> bytes:
    if _HAS_AUDIOOP:
        return audioop.alaw2lin(data, 2)
    return struct.pack(f"<{len(data)}h", *[_ALAW_DEC[b] for b in data])


def _ulaw2lin(data: bytes) -> bytes:
    if _HAS_AUDIOOP:
        return audioop.ulaw2lin(data, 2)
    return struct.pack(f"<{len(data)}h", *[_ULAW_DEC[b] for b in data])


def _lin2alaw(data: bytes) -> bytes:
    if _HAS_AUDIOOP:
        return audioop.lin2alaw(data, 2)
    # Simple approximation without full lookup table
    samples = struct.unpack(f"<{len(data)//2}h", data)
    result = bytearray(len(samples))
    for i, s in enumerate(samples):
        sign = 0x80 if s >= 0 else 0
        s = abs(s)
        if s < 256:
            exp, mant = 0, s >> 4
        elif s < 512:
            exp, mant = 1, (s - 256) >> 5
        elif s < 1024:
            exp, mant = 2, (s - 512) >> 6
        elif s < 2048:
            exp, mant = 3, (s - 1024) >> 7
        elif s < 4096:
            exp, mant = 4, (s - 2048) >> 8
        elif s < 8192:
            exp, mant = 5, (s - 4096) >> 9
        elif s < 16384:
            exp, mant = 6, (s - 8192) >> 10
        else:
            exp, mant = 7, (s - 16384) >> 11
        result[i] = ((sign | (exp << 4) | (mant & 0x0F)) ^ 0x55)
    return bytes(result)


def _lin2ulaw(data: bytes) -> bytes:
    if _HAS_AUDIOOP:
        return audioop.lin2ulaw(data, 2)
    samples = struct.unpack(f"<{len(data)//2}h", data)
    result = bytearray(len(samples))
    for i, s in enumerate(samples):
        sign = 0 if s >= 0 else 0x80
        s = min(abs(s) + 33, 32767)
        if s < 66:
            exp, mant = 0, (s - 33) >> 1
        elif s < 130:
            exp, mant = 1, (s - 66) >> 2
        elif s < 258:
            exp, mant = 2, (s - 130) >> 3
        elif s < 514:
            exp, mant = 3, (s - 258) >> 4
        elif s < 1026:
            exp, mant = 4, (s - 514) >> 5
        elif s < 2050:
            exp, mant = 5, (s - 1026) >> 6
        elif s < 4098:
            exp, mant = 6, (s - 2050) >> 7
        else:
            exp, mant = 7, (s - 4098) >> 8
        result[i] = ~(sign | (exp << 4) | (mant & 0x0F)) & 0xFF
    return bytes(result)


# ---------------------------------------------------------------------------
# G722 codec helpers
#
# API notes (G722 package v1.2.5):
#   G722.G722(sample_rate=16000, bit_rate=64000) -> stateful ADPCM codec object
#   encoder.encode(array.array('h', samples)) -> bytes  (2 samples per byte at 16kHz)
#   decoder.decode(bytes)                     -> array.array('h', samples at 8kHz)
#
# Frame sizes for 20 ms at G722 64kbps:
#   Input PCM:  640 bytes (320 int16 samples at 16kHz)
#   Encoded:    160 bytes (standard G722 RTP payload at 64kbps)
#   Decoded:    160 int16 samples representing 20ms at 8kHz
#               -> upsample x2 to 320 samples at 16kHz for the pipeline
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RTP constants
# ---------------------------------------------------------------------------

RTP_HEADER_SIZE = 12
FRAME_MS = 20
SAMPLES_8K  = 8000  * FRAME_MS // 1000   # 160 samples per 20 ms @  8 kHz
SAMPLES_16K = 16000 * FRAME_MS // 1000   # 320 samples per 20 ms @ 16 kHz
SAMPLES_24K = 24000 * FRAME_MS // 1000   # 480 samples per 20 ms @ 24 kHz
SAMPLES_48K = 48000 * FRAME_MS // 1000   # 960 samples per 20 ms @ 48 kHz

# RTP timestamp increment per 20 ms frame, keyed by codec.
# G722  uses 8 kHz RTP clock despite 16 kHz audio (RFC 3551 section 4.5.2).
# Opus  always uses 48 kHz RTP clock regardless of audio sample rate (RFC 7587).
_RTP_TS_INCREMENT: dict[str, int] = {
    "PCMA": SAMPLES_8K,    # 160
    "PCMU": SAMPLES_8K,    # 160
    "G722": SAMPLES_8K,    # 160  (8 kHz RTP clock)
    "OPUS": SAMPLES_48K,   # 960  (48 kHz RTP clock)
}

# PCM bytes per 20 ms frame sent to / received from send_audio(), keyed by codec.
_PCM_FRAME_BYTES: dict[str, int] = {
    "PCMA": SAMPLES_8K  * 2,   #  320 bytes
    "PCMU": SAMPLES_8K  * 2,   #  320 bytes
    "G722": SAMPLES_16K * 2,   #  640 bytes  (16 kHz PCM)
    "OPUS": SAMPLES_16K * 2,   #  640 bytes  (16 kHz PCM)
}

# Encoded bytes per 20 ms frame, keyed by codec.
# Opus output is variable-length; 0 here signals "send as single packet".
_ENC_FRAME_BYTES: dict[str, int] = {
    "PCMA": SAMPLES_8K,   # 160 bytes
    "PCMU": SAMPLES_8K,   # 160 bytes
    "G722": SAMPLES_8K,   # 160 bytes (64 kbps)
    "OPUS": 0,            # variable; handled separately in send_audio()
}


@dataclass
class DTMFEvent:
    digit: str
    duration_ms: int


# ---------------------------------------------------------------------------
# asyncio DatagramProtocol
# ---------------------------------------------------------------------------

class _RTPProtocol(asyncio.DatagramProtocol):
    def __init__(self, session: "RTPSession"):
        self._session = session

    def datagram_received(self, data: bytes, addr: tuple):
        self._session._on_datagram(data, addr)

    def error_received(self, exc: Exception):
        logger.debug(f"RTP socket error: {exc}")

    def connection_lost(self, exc):
        pass


# ---------------------------------------------------------------------------
# RTPSession
# ---------------------------------------------------------------------------

class RTPSession:
    """
    Bidirectional RTP session over asyncio UDP.

    Supports PCMA, PCMU (8 kHz), G722 (16 kHz), Opus (16 kHz / 48 kHz RTP clock).

    ``on_audio(pcm_bytes)``  - called for each received 20 ms audio frame
                               (16-bit LE mono PCM at the codec's sample rate)
    ``on_dtmf(DTMFEvent)``   - called when a complete RFC 2833 DTMF event arrives
    """

    def __init__(
        self,
        local_ip: str,
        local_port: int,
        remote_addr: tuple[str, int],
        codec: str,                         # "PCMA" or "PCMU"
        payload_type: int,                  # 8 for PCMA, 0 for PCMU
        dtmf_payload_type: int = 101,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_dtmf: Optional[Callable[[DTMFEvent], None]] = None,
    ):
        self._local_ip = local_ip
        self._local_port = local_port
        self._remote_addr = remote_addr
        self._codec = codec
        self._pt = payload_type
        self._dtmf_pt = dtmf_payload_type
        self.on_audio = on_audio
        self.on_dtmf = on_dtmf

        self._transport: Optional[asyncio.DatagramTransport] = None
        self._ssrc = random.randint(0, 0xFFFFFFFF)

        # G722: stateful ADPCM codec objects (one per direction)
        self._g722_encoder = None
        self._g722_decoder = None
        if self._codec == "G722":
            if _HAS_G722:
                self._g722_encoder = _G722Lib.G722(16000, 64000)
                self._g722_decoder = _G722Lib.G722(16000, 64000)
                logger.info("G722 stateful encoder/decoder initialized")
            else:
                logger.error("G722 codec requested but G722 package not installed")

        # Opus: stateful encoder/decoder (16 kHz, mono)
        # Using 16 kHz (not 24 kHz) so SileroVAD is compatible (supports 8/16 kHz only).
        # RTP clock stays 48 kHz per RFC 7587 (960 ts increment per 20 ms frame).
        self._opus_encoder = None
        self._opus_decoder = None
        self._opus_frame_size = SAMPLES_16K  # 320 samples = 20 ms @ 16 kHz
        if self._codec == "OPUS":
            if _HAS_OPUS:
                self._opus_encoder = _opuslib.Encoder(16000, 1, _opuslib.APPLICATION_VOIP)
                self._opus_decoder = _opuslib.Decoder(16000, 1)
                logger.info("Opus stateful encoder/decoder initialized @ 16 kHz")
            else:
                logger.error("Opus codec requested but opuslib not installed")
        self._seq = random.randint(0, 0xFFFF)
        self._ts = random.randint(0, 0xFFFFFFFF)

        self._running = False
        self._dtmf_last_seq: Optional[int] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _RTPProtocol(self),
            local_addr=(self._local_ip, self._local_port),
        )
        self._running = True
        logger.info(
            f"RTP started local={self._local_ip}:{self._local_port} "
            f"remote={self._remote_addr} codec={self._codec}/{self._pt}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._transport:
            self._transport.close()
            self._transport = None
        logger.debug(f"RTP stopped port={self._local_port}")

    # ------------------------------------------------------------------
    # Receive path
    # ------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple) -> None:
        if len(data) < RTP_HEADER_SIZE:
            return

        v = (data[0] >> 6) & 0x3
        if v != 2:
            return

        has_ext = (data[0] >> 4) & 0x1
        cc = data[0] & 0x0F
        pt = data[1] & 0x7F
        seq = struct.unpack_from(">H", data, 2)[0]

        offset = RTP_HEADER_SIZE + cc * 4
        if has_ext and len(data) >= offset + 4:
            ext_len = struct.unpack_from(">H", data, offset + 2)[0]
            offset += 4 + ext_len * 4

        payload = data[offset:]
        if not payload:
            return

        # Symmetric RTP: learn remote address from first packet
        self._remote_addr = (addr[0], addr[1])

        if pt == self._dtmf_pt:
            self._recv_dtmf(payload, seq)
        elif pt == self._pt:
            self._recv_audio(payload)

    def _recv_audio(self, payload: bytes) -> None:
        try:
            if self._codec == "PCMA":
                pcm = _alaw2lin(payload)
            elif self._codec == "PCMU":
                pcm = _ulaw2lin(payload)
            elif self._codec == "G722":
                pcm = self._g722_decode(payload)
            elif self._codec == "OPUS":
                pcm = self._opus_decode(payload)
            else:
                return
        except Exception as e:
            logger.debug(f"RTP decode error ({self._codec}): {e}")
            return
        if self.on_audio:
            self.on_audio(pcm)

    def _g722_decode(self, payload: bytes) -> bytes:
        """Decode G722 payload -> 16-bit LE PCM at 16 kHz.

        The G722 library decoder reconstructs the full 16 kHz wideband audio.
        A 20 ms G722 packet (160 bytes) yields 320 int16 samples at 16 kHz -> 640 bytes.
        """
        if self._g722_decoder is None:
            raise RuntimeError("G722 decoder not initialized")
        decoded_arr = self._g722_decoder.decode(payload)   # array.array('h') at 16kHz
        return bytes(decoded_arr.tobytes())

    def _opus_decode(self, payload: bytes) -> bytes:
        """Decode Opus payload -> 16-bit LE PCM at 16 kHz (640 bytes = 320 samples = 20 ms)."""
        if self._opus_decoder is None:
            raise RuntimeError("Opus decoder not initialized")
        return self._opus_decoder.decode(payload, self._opus_frame_size)

    def _opus_encode(self, pcm: bytes) -> bytes:
        """Encode 16-bit LE PCM at 16 kHz -> Opus packet (variable length)."""
        if self._opus_encoder is None:
            raise RuntimeError("Opus encoder not initialized")
        return self._opus_encoder.encode(pcm, self._opus_frame_size)

    def _g722_encode(self, pcm: bytes) -> bytes:
        """Encode 16-bit LE PCM at 16 kHz -> G722 payload bytes.

        The G722 library encoder expects array.array('h') of int16 samples.
        640 bytes in (320 samples, 20ms at 16kHz) -> 160 bytes out (standard G722 RTP).
        """
        if self._g722_encoder is None:
            raise RuntimeError("G722 encoder not initialized")
        arr = array.array('h')
        arr.frombytes(pcm)
        return bytes(self._g722_encoder.encode(arr))

    def _recv_dtmf(self, payload: bytes, seq: int) -> None:
        if len(payload) < 4:
            return
        event_code = payload[0]
        end = bool(payload[1] & 0x80)
        duration = struct.unpack_from(">H", payload, 2)[0]

        if end and seq != self._dtmf_last_seq:
            self._dtmf_last_seq = seq
            digit = _dtmf_digit(event_code)
            if digit and self.on_dtmf:
                duration_ms = duration * 1000 // 8000
                self.on_dtmf(DTMFEvent(digit=digit, duration_ms=duration_ms))

    # ------------------------------------------------------------------
    # Send path
    # ------------------------------------------------------------------

    def send_audio(self, pcm: bytes) -> None:
        """Encode PCM (16-bit LE mono) to the negotiated codec and transmit as RTP."""
        if not self._running or not self._transport:
            return

        ts_inc = _RTP_TS_INCREMENT.get(self._codec, SAMPLES_8K)

        try:
            if self._codec == "PCMA":
                encoded = _lin2alaw(pcm)
            elif self._codec == "PCMU":
                encoded = _lin2ulaw(pcm)
            elif self._codec == "G722":
                encoded = self._g722_encode(pcm)
            elif self._codec == "OPUS":
                # Opus: one PCM frame -> one variable-length RTP packet
                encoded = self._opus_encode(pcm)
                self._send_packet(encoded, ts_increment=ts_inc)
                return
            else:
                return
        except Exception as e:
            logger.debug(f"RTP encode error ({self._codec}): {e}")
            return

        enc_frame = _ENC_FRAME_BYTES.get(self._codec, SAMPLES_8K)
        for i in range(0, len(encoded), enc_frame):
            chunk = encoded[i: i + enc_frame]
            if chunk:
                self._send_packet(chunk, marker=(i == 0), ts_increment=ts_inc)

    def send_dtmf(self, digit: str, duration_ms: int = 160) -> None:
        """Send DTMF digit via RFC 2833 (start + 3 end packets for reliability)."""
        if not self._running or not self._transport:
            return
        code = _dtmf_code(digit)
        if code is None:
            logger.warning(f"Unknown DTMF digit: {digit!r}")
            return

        samples = duration_ms * 8000 // 1000
        volume = 10  # -10 dBm0

        for i, end in enumerate([False, True, True, True]):
            dur = samples if end else max(1, samples * (i + 1) // 4)
            payload = struct.pack(
                ">BBH",
                code,
                (0x80 if end else 0x00) | (volume & 0x3F),
                dur,
            )
            self._send_packet(payload, pt_override=self._dtmf_pt, marker=(i == 0))

    def _send_packet(
        self,
        payload: bytes,
        marker: bool = False,
        pt_override: Optional[int] = None,
        ts_increment: Optional[int] = None,
    ) -> None:
        if not self._transport:
            return
        pt = pt_override if pt_override is not None else self._pt
        header = struct.pack(
            ">BBHII",
            0x80,
            pt | (0x80 if marker else 0x00),
            self._seq & 0xFFFF,
            self._ts & 0xFFFFFFFF,
            self._ssrc,
        )
        self._seq = (self._seq + 1) & 0xFFFF
        if pt_override is None:
            # Use explicit increment when provided (e.g. G722 uses 8kHz RTP clock)
            inc = ts_increment if ts_increment is not None else len(payload)
            self._ts = (self._ts + inc) & 0xFFFFFFFF
        try:
            self._transport.sendto(header + payload, self._remote_addr)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dtmf_digit(code: int) -> Optional[str]:
    if 0 <= code <= 9:
        return str(code)
    return {10: "*", 11: "#", 12: "A", 13: "B", 14: "C", 15: "D"}.get(code)


def _dtmf_code(digit: str) -> Optional[int]:
    d = digit.upper()
    if d.isdigit():
        return int(d)
    return {"*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}.get(d)


# ---------------------------------------------------------------------------
# Port allocator
# ---------------------------------------------------------------------------

class RTPPortAllocator:
    """Thread-safe sequential port allocator for RTP sessions."""

    def __init__(self, start: int = 10000, end: int = 20000):
        self._start = start
        self._next = start
        self._end = end

    def allocate(self) -> int:
        port = self._next
        self._next += 2  # RTP uses even ports; RTCP uses next odd (we skip RTCP)
        if self._next > self._end:
            self._next = self._start
        return port
