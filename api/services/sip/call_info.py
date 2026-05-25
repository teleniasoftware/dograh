"""Per-call metadata extracted from SIP INVITE."""

from dataclasses import dataclass, field


@dataclass
class SIPCallInfo:
    """
    All information available from a SIP INVITE, passed to the agent session.

    Available immediately at call establishment; no pre-call registration needed.
    All SIP headers (including X-* custom headers) are in ``all_headers``.
    """

    # Session identifiers
    session_id: int               # monotonic counter from SIPServer
    call_uuid: str                # UUIDv4 generated per call

    # SIP dialog identifiers
    sip_call_id: str              # SIP Call-ID header
    from_uri: str                 # sip:+391234567890@pbx.example.com
    to_uri: str                   # sip:centralino@voice-bot.example.com
    caller_number: str            # E.164 or local: +391234567890
    callee_number: str            # DID or local extension: centralino
    agent_id: str                 # derived from To URI user part; maps to workflow_uuid

    # Audio parameters
    codec: str                    # "PCMA" (G.711 alaw) or "PCMU" (G.711 ulaw)
    sample_rate: int              # always 8000 for G.711
    rtp_local_port: int           # our RTP listening port
    rtp_remote_addr: tuple        # (ip, port) of the remote RTP endpoint

    # DTMF
    dtmf_payload_type: int = 101  # RFC 2833 payload type

    # All SIP headers from the INVITE (From, To, X-Call-ID, X-*, ...)
    all_headers: dict = field(default_factory=dict)
