"""Runtime registry for browser-driven SIP test calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

TestSender = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class SIPTestSession:
    workflow_id: int
    workflow_uuid: str
    organization_id: int
    user_id: int
    sender: TestSender


_sessions: dict[str, SIPTestSession] = {}


def register_sip_test_session(
    test_session_id: str,
    *,
    workflow_id: int,
    workflow_uuid: str,
    organization_id: int,
    user_id: int,
    sender: TestSender,
) -> None:
    _sessions[test_session_id] = SIPTestSession(
        workflow_id=workflow_id,
        workflow_uuid=workflow_uuid,
        organization_id=organization_id,
        user_id=user_id,
        sender=sender,
    )


def unregister_sip_test_sender(test_session_id: str) -> None:
    _sessions.pop(test_session_id, None)


def get_sip_test_session(test_session_id: str) -> Optional[SIPTestSession]:
    return _sessions.get(test_session_id)


def get_sip_test_sender(test_session_id: str) -> Optional[TestSender]:
    session = _sessions.get(test_session_id)
    return session.sender if session else None
