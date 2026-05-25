"""
Minimal SIP message parser and builder.

Handles: INVITE, ACK, BYE, CANCEL, REFER, OPTIONS, NOTIFY requests
         and 1xx/2xx/4xx/5xx responses.

Only the subset of SIP needed for audio call setup/teardown and blind transfer.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# Headers that may appear multiple times in one message.
_MULTI_HEADERS = frozenset({"via", "v", "route", "record-route"})


@dataclass
class SIPMessage:
    """Parsed SIP request or response."""

    # Request fields (None for responses)
    method: Optional[str] = None
    request_uri: Optional[str] = None

    # Response fields (None for requests)
    status_code: Optional[int] = None
    reason: Optional[str] = None

    # Single-value headers (lowercase keys)
    headers: dict[str, str] = field(default_factory=dict)
    # Multi-value headers (Via, Route, etc.)
    headers_multi: dict[str, list[str]] = field(default_factory=dict)

    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def call_id(self) -> str:
        return self.headers.get("call-id", self.headers.get("i", ""))

    @property
    def from_header(self) -> str:
        return self.headers.get("from", self.headers.get("f", ""))

    @property
    def to_header(self) -> str:
        return self.headers.get("to", self.headers.get("t", ""))

    @property
    def cseq(self) -> tuple[int, str]:
        val = self.headers.get("cseq", "1 INVITE")
        parts = val.strip().split(None, 1)
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")

    @property
    def via_list(self) -> list[str]:
        return self.headers_multi.get("via", self.headers_multi.get("v", []))

    @property
    def contact(self) -> str:
        return self.headers.get("contact", self.headers.get("m", ""))

    def get(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


def parse_message(data: bytes) -> Optional[SIPMessage]:
    """Parse raw UDP payload into a SIPMessage. Returns None on failure."""
    try:
        sep = b"\r\n\r\n"
        if sep in data:
            header_bytes, body = data.split(sep, 1)
        else:
            header_bytes, body = data, b""

        lines = header_bytes.decode("utf-8", errors="replace").split("\r\n")
        if not lines:
            return None

        msg = SIPMessage(body=body)
        first = lines[0].strip()

        if first.startswith("SIP/2.0 "):
            parts = first.split(" ", 2)
            msg.status_code = int(parts[1])
            msg.reason = parts[2] if len(parts) > 2 else ""
        else:
            parts = first.split(" ", 2)
            if len(parts) < 2:
                return None
            msg.method = parts[0].upper()
            msg.request_uri = parts[1]

        for line in lines[1:]:
            if not line.strip() or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in _MULTI_HEADERS:
                msg.headers_multi.setdefault(key, []).append(val)
                if key not in msg.headers:
                    msg.headers[key] = val
            else:
                msg.headers[key] = val

        return msg
    except Exception:
        return None


# ---------------------------------------------------------------------------
# URI / header helpers
# ---------------------------------------------------------------------------

def extract_uri(header: str) -> str:
    """Return the SIP URI from a header like 'Name <sip:user@host>;tag=x'."""
    m = re.search(r"<([^>]+)>", header)
    if m:
        return m.group(1)
    return header.split(";")[0].strip()


def extract_tag(header: str) -> str:
    m = re.search(r";tag=([^\s;]+)", header)
    return m.group(1) if m else ""


def extract_user(uri: str) -> str:
    """Return user part from sip:user@host or the whole string."""
    m = re.match(r"sips?:([^@;>\s]+)", uri, re.IGNORECASE)
    return m.group(1) if m else uri


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

_STATUS_REASONS = {
    100: "Trying", 180: "Ringing", 183: "Session Progress",
    200: "OK", 202: "Accepted",
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
    403: "Forbidden", 404: "Not Found", 408: "Request Timeout",
    480: "Temporarily Unavailable", 486: "Busy Here", 487: "Request Terminated",
    500: "Server Internal Error", 503: "Service Unavailable",
}


def build_response(
    request: SIPMessage,
    status: int,
    local_tag: str = "",
    body: bytes = b"",
    content_type: str = "application/sdp",
    extra: Optional[dict[str, str]] = None,
) -> bytes:
    """Build a SIP response to *request*."""
    reason = _STATUS_REASONS.get(status, "Unknown")
    lines = [f"SIP/2.0 {status} {reason}"]

    for via in request.via_list:
        lines.append(f"Via: {via}")

    lines.append(f"From: {request.from_header}")

    to_val = request.to_header
    if status >= 200 and local_tag and ";tag=" not in to_val:
        to_val += f";tag={local_tag}"
    lines.append(f"To: {to_val}")

    lines.append(f"Call-ID: {request.call_id}")
    lines.append(f"CSeq: {request.headers.get('cseq', '1 INVITE')}")

    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")

    if body:
        lines.append(f"Content-Type: {content_type}")
        lines.append(f"Content-Length: {len(body)}")
    else:
        lines.append("Content-Length: 0")

    lines.append("")
    return "\r\n".join(lines).encode() + b"\r\n" + body


def build_request(
    method: str,
    request_uri: str,
    from_tag: str,
    to_tag: str,
    from_uri: str,
    to_uri: str,
    call_id: str,
    cseq: int,
    via: str,
    contact: str,
    body: bytes = b"",
    content_type: str = "application/sdp",
    extra: Optional[dict[str, str]] = None,
) -> bytes:
    """Build a SIP request."""
    lines = [f"{method} {request_uri} SIP/2.0"]
    lines.append(f"Via: {via}")

    from_val = from_uri if "<" in from_uri else f"<{from_uri}>"
    if from_tag:
        from_val += f";tag={from_tag}"
    lines.append(f"From: {from_val}")

    to_val = to_uri if "<" in to_uri else f"<{to_uri}>"
    if to_tag:
        to_val += f";tag={to_tag}"
    lines.append(f"To: {to_val}")

    lines.append(f"Call-ID: {call_id}")
    lines.append(f"CSeq: {cseq} {method}")
    lines.append(f"Contact: {contact}")
    lines.append("Max-Forwards: 70")
    lines.append("User-Agent: VoiceBot/1.0")
    lines.append("Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REFER, NOTIFY")

    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")

    if body:
        lines.append(f"Content-Type: {content_type}")
        lines.append(f"Content-Length: {len(body)}")
    else:
        lines.append("Content-Length: 0")

    lines.append("")
    return "\r\n".join(lines).encode() + b"\r\n" + body
