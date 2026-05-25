"""
SDP parser and builder for audio-only SIP calls.

Supported codecs (in preference order):
  G722  (PT=9,  16 kHz wideband, RTP clock 8000 Hz per RFC 3551)
  PCMA  (PT=8,  G.711 alaw,  8 kHz)
  PCMU  (PT=0,  G.711 ulaw,  8 kHz)

telephone-event (RFC 2833 DTMF, PT=101 by default) is always included.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


TELEPHONE_EVENT_PT = 101

# Maps codec name -> (audio_sample_rate, rtp_clock_rate)
# G722 is 16 kHz audio but uses 8000 Hz RTP clock (RFC 3551 section 4.5.2).
# Opus always uses 48000 Hz RTP clock (RFC 7587) even when audio is 24 kHz.
CODEC_PARAMS: dict[str, tuple[int, int]] = {
    "OPUS": (16000, 48000),
    "G722": (16000, 8000),
    "PCMA": (8000, 8000),
    "PCMU": (8000, 8000),
}

# Preference order for codec selection (best quality first).
# A codec is only included when its Python package is installed.
_pref: list[str] = []
try:
    import opuslib as _opuslib_pkg  # noqa: F401
    _pref.append("OPUS")
except ImportError:
    pass
try:
    import G722 as _g722_pkg  # noqa: F401  (module name is capital G722)
    _pref.append("G722")
except ImportError:
    pass
_pref += ["PCMA", "PCMU"]
_CODEC_PREFERENCE: list[str] = _pref


@dataclass
class AudioMedia:
    port: int
    payload_types: list[int] = field(default_factory=list)
    rtpmap: dict[int, str] = field(default_factory=dict)   # pt -> "PCMA/8000"
    fmtp: dict[int, str] = field(default_factory=dict)
    direction: str = "sendrecv"
    connection_ip: str = ""                                 # from c= in media section


@dataclass
class SDPSession:
    connection_ip: str = ""
    session_id: str = "0"
    audio: Optional[AudioMedia] = None


def parse(body: bytes) -> SDPSession:
    """Parse SDP body bytes into SDPSession."""
    sdp = SDPSession()
    media: Optional[AudioMedia] = None

    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")

        if key == "o":
            parts = value.split()
            sdp.session_id = parts[1] if len(parts) > 1 else "0"

        elif key == "c":
            # c=IN IP4 192.168.1.1
            parts = value.split()
            ip = parts[2] if len(parts) > 2 else ""
            if media:
                media.connection_ip = ip
            else:
                sdp.connection_ip = ip

        elif key == "m":
            # m=audio 10000 RTP/AVP 0 8 101
            parts = value.split()
            if parts[0] == "audio" and len(parts) >= 3:
                pts = [int(p) for p in parts[3:] if p.isdigit()]
                media = AudioMedia(port=int(parts[1]), payload_types=pts)
                sdp.audio = media

        elif key == "a" and media:
            if value.startswith("rtpmap:"):
                m = re.match(r"rtpmap:(\d+)\s+(.+)", value)
                if m:
                    media.rtpmap[int(m.group(1))] = m.group(2)
            elif value.startswith("fmtp:"):
                m = re.match(r"fmtp:(\d+)\s+(.+)", value)
                if m:
                    media.fmtp[int(m.group(1))] = m.group(2)
            elif value in ("sendrecv", "sendonly", "recvonly", "inactive"):
                media.direction = value

    return sdp


def select_codec(remote: SDPSession) -> tuple[int, str]:
    """
    Choose the best codec from the remote SDP.

    Preference order: G722 > PCMA > PCMU.
    Returns (payload_type, codec_name).
    """
    if not remote.audio:
        return 8, "PCMA"

    rtpmap = remote.audio.rtpmap
    pts = remote.audio.payload_types

    # Build a map: canonical_codec_name -> (pt, canonical_name)
    offered: dict[str, tuple[int, str]] = {}
    for pt, desc in rtpmap.items():
        name = desc.split("/")[0].upper()
        canonical = "OPUS" if name == "OPUS" else name
        if canonical not in offered:
            offered[canonical] = (pt, canonical)

    # Also handle static payload types without rtpmap entries
    if 9 not in rtpmap and 9 in pts:
        offered.setdefault("G722", (9, "G722"))
    if 8 not in rtpmap and 8 in pts:
        offered.setdefault("PCMA", (8, "PCMA"))
    if 0 not in rtpmap and 0 in pts:
        offered.setdefault("PCMU", (0, "PCMU"))

    for codec in _CODEC_PREFERENCE:
        if codec in offered:
            pt, name = offered[codec]
            return pt, name

    return 8, "PCMA"


def find_dtmf_pt(remote: SDPSession) -> Optional[int]:
    """Return the telephone-event payload type advertised by the remote, or None."""
    if not remote.audio:
        return None
    for pt, desc in remote.audio.rtpmap.items():
        if "telephone-event" in desc.lower():
            return pt
    return None


def build(
    local_ip: str,
    rtp_port: int,
    codec_pt: int,
    codec_name: str,
    sample_rate: int = 8000,
    dtmf_pt: int = TELEPHONE_EVENT_PT,
    session_id: str = "1",
    direction: str = "sendrecv",
) -> bytes:
    """Build an SDP answer for an audio-only session.

    ``sample_rate`` is unused (kept for API compatibility); the RTP clock and
    codec-specific SDP attributes are derived from CODEC_PARAMS.
    """
    audio_rate, rtp_clock = CODEC_PARAMS.get(codec_name.upper(), (sample_rate, sample_rate))
    upper = codec_name.upper()

    lines = [
        "v=0",
        f"o=voicebot {session_id} {session_id} IN IP4 {local_ip}",
        "s=VoiceBot",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {rtp_port} RTP/AVP {codec_pt} {dtmf_pt}",
    ]

    if upper == "OPUS":
        # RFC 7587: rtpmap always uses 48000/2 (stereo channel count is mandatory)
        lines.append(f"a=rtpmap:{codec_pt} opus/48000/2")
        lines.append(
            f"a=fmtp:{codec_pt} maxplaybackrate={audio_rate};"
            f" sprop-maxcapturerate={audio_rate}; stereo=0; sprop-stereo=0"
        )
    else:
        lines.append(f"a=rtpmap:{codec_pt} {codec_name}/{rtp_clock}")

    lines += [
        f"a=rtpmap:{dtmf_pt} telephone-event/8000",
        f"a=fmtp:{dtmf_pt} 0-15",
        f"a={direction}",
    ]
    return "\r\n".join(lines).encode() + b"\r\n"
