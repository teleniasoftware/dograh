from api.services.sip.message import build_response, extract_user, parse_message
from api.services.sip.rtp_session import RTPPortAllocator
from api.services.sip.sdp import build, find_dtmf_pt, parse, select_codec


def test_sip_message_parse_invite_and_build_response():
    raw = (
        b"INVITE sip:agent-uuid@127.0.0.1:5090 SIP/2.0\r\n"
        b"Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bKabc\r\n"
        b"From: <sip:+39123@host>;tag=caller-tag\r\n"
        b"To: <sip:agent-uuid@127.0.0.1:5090>\r\n"
        b"Call-ID: call-001\r\n"
        b"CSeq: 1 INVITE\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )

    msg = parse_message(raw)

    assert msg is not None
    assert msg.method == "INVITE"
    assert msg.call_id == "call-001"
    assert extract_user(msg.request_uri) == "agent-uuid"

    response = build_response(msg, 404, local_tag="srv")
    assert b"SIP/2.0 404 Not Found" in response
    assert b"Call-ID: call-001" in response


def test_sdp_selects_g711_and_dtmf_payload():
    payload = (
        b"v=0\r\n"
        b"o=- 1 1 IN IP4 10.0.0.1\r\n"
        b"c=IN IP4 10.0.0.1\r\n"
        b"t=0 0\r\n"
        b"m=audio 10000 RTP/AVP 0 8 101\r\n"
        b"a=rtpmap:0 PCMU/8000\r\n"
        b"a=rtpmap:8 PCMA/8000\r\n"
        b"a=rtpmap:101 telephone-event/8000\r\n"
        b"a=fmtp:101 0-15\r\n"
    )

    sdp = parse(payload)
    payload_type, codec = select_codec(sdp)

    assert payload_type == 8
    assert codec == "PCMA"
    assert find_dtmf_pt(sdp) == 101


def test_sdp_builds_answer_with_agent_rtp_port():
    answer = build(
        local_ip="127.0.0.1",
        rtp_port=12000,
        codec_pt=8,
        codec_name="PCMA",
        session_id="42",
    )

    assert b"o=voicebot 42 42" in answer
    assert b"m=audio 12000 RTP/AVP 8 101" in answer
    assert b"a=rtpmap:8 PCMA/8000" in answer


def test_rtp_port_allocator_wraps_to_configured_start():
    allocator = RTPPortAllocator(start=12000, end=12002)

    assert allocator.allocate() == 12000
    assert allocator.allocate() == 12002
    assert allocator.allocate() == 12000
