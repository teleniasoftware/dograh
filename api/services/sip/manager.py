"""Lifecycle manager for Dograh native SIP inbound calls."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger
from pipecat.utils.run_context import set_current_org_id, set_current_run_id

from api.db import db_client
from api.enums import CallType, WorkflowRunMode, WorkflowRunState, WorkflowStatus
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.ws_sender_registry import (
    register_ws_sender,
    unregister_ws_sender,
)
from api.services.quota_service import check_dograh_quota_by_user_id
from api.services.sip.call_info import SIPCallInfo
from api.services.sip.server import SIPServer
from api.services.sip.test_registry import get_sip_test_sender, get_sip_test_session
from api.services.sip.transport import SIPTransport


@dataclass
class _PreflightTarget:
    workflow_id: int
    workflow_uuid: str
    organization_id: int
    user_id: int
    sip_test_session_id: str | None = None
    use_draft: bool = False


@dataclass
class _ActiveSession:
    call_id: str
    workflow_run_id: int
    transport: SIPTransport
    task: asyncio.Task
    sip_test_session_id: str | None = None


class SIPIngressManager:
    """Owns the SIP UDP server and maps SIP calls to Dograh workflow runs."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 5090,
        rtp_start_port: int = 10000,
        rtp_end_port: int = 10400,
        max_concurrent_calls: int = 100,
        auth_username: str = "",
        auth_password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._max_concurrent_calls = max_concurrent_calls
        self._server = SIPServer(
            host=host,
            port=port,
            rtp_start_port=rtp_start_port,
            rtp_end_port=rtp_end_port,
            on_pre_call=self._on_pre_call,
            on_call_established=self._on_call_established,
            on_call_terminated=self._on_call_terminated,
            auth_username=auth_username,
            auth_password=auth_password,
        )
        self._preflight: dict[str, _PreflightTarget] = {}
        self._sessions: dict[str, _ActiveSession] = {}
        self._ending_calls: set[str] = set()
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self._server.start()
        logger.info(
            "SIP ingress listening on {}:{} UDP; dial sip:<agent_uuid>@host:{}",
            self._host,
            self._port,
            self._port,
        )

    async def stop(self) -> None:
        self._running = False
        await self._server.stop()
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._preflight.clear()
        for session in sessions:
            await self._stop_session(session)
        logger.info("SIP ingress stopped")

    async def _on_pre_call(
        self, callee: str, call_id: str, headers: dict[str, str]
    ) -> Optional[int]:
        """Validate the callee Agent UUID before answering the SIP INVITE."""
        if len(self._sessions) >= self._max_concurrent_calls:
            return 486

        agent_uuid = callee.strip()
        if not agent_uuid:
            return 404

        sip_test_session_id = headers.get("x-dograh-sip-test-session") or None
        sip_test_session = (
            get_sip_test_session(sip_test_session_id) if sip_test_session_id else None
        )
        if sip_test_session:
            if sip_test_session.workflow_uuid != agent_uuid:
                logger.warning(
                    "SIP test session {} rejected: callee {} did not match workflow {}",
                    sip_test_session_id,
                    agent_uuid,
                    sip_test_session.workflow_uuid,
                )
                return 404

            quota = await check_dograh_quota_by_user_id(
                sip_test_session.user_id,
                workflow_id=sip_test_session.workflow_id,
            )
            if not quota.has_quota:
                logger.warning(
                    "SIP test callee {} rejected for quota: {}",
                    agent_uuid,
                    quota.error_message,
                )
                return 402

            self._preflight[call_id] = _PreflightTarget(
                workflow_id=sip_test_session.workflow_id,
                workflow_uuid=sip_test_session.workflow_uuid,
                organization_id=sip_test_session.organization_id,
                user_id=sip_test_session.user_id,
                sip_test_session_id=sip_test_session_id,
                use_draft=True,
            )
            return None

        workflow = await db_client.get_workflow_by_uuid_unscoped(agent_uuid)
        if not workflow:
            logger.warning("SIP callee {} did not match any workflow_uuid", agent_uuid)
            return 404

        if workflow.status != WorkflowStatus.ACTIVE.value:
            logger.warning("SIP callee {} matched non-active workflow", agent_uuid)
            return 404

        if workflow.user_id is None:
            logger.warning("SIP callee {} workflow has no execution owner", agent_uuid)
            return 503

        if workflow.released_definition is None:
            logger.warning("SIP callee {} workflow has no published definition", agent_uuid)
            return 404

        quota = await check_dograh_quota_by_user_id(
            workflow.user_id,
            workflow_id=workflow.id,
        )
        if not quota.has_quota:
            logger.warning(
                "SIP callee {} rejected for quota: {}",
                agent_uuid,
                quota.error_message,
            )
            return 402

        self._preflight[call_id] = _PreflightTarget(
            workflow_id=workflow.id,
            workflow_uuid=workflow.workflow_uuid,
            organization_id=workflow.organization_id,
            user_id=workflow.user_id,
            sip_test_session_id=sip_test_session_id,
        )
        return None

    async def _on_call_established(self, info: SIPCallInfo) -> None:
        target = self._preflight.get(info.sip_call_id)
        if target is None:
            logger.warning("SIP call {} established without preflight", info.sip_call_id)
            dialog = self._server.get_dialog(info.sip_call_id)
            if dialog:
                await dialog.hangup(reason_code=404)
            return

        if info.sample_rate not in (8000, 16000):
            logger.warning(
                "Unsupported SIP sample rate {} for call {}; falling back to 8000",
                info.sample_rate,
                info.sip_call_id,
            )
            info.sample_rate = 8000

        numeric_suffix = int(str(uuid.uuid4()).replace("-", "")[:8], 16) % 100000000
        workflow_run_name = f"WR-SIP-IN-{numeric_suffix:08d}"
        dialog = self._server.get_dialog(info.sip_call_id)
        if dialog is None or dialog.rtp_session is None:
            logger.error("SIP call {} has no RTP session", info.sip_call_id)
            self._preflight.pop(info.sip_call_id, None)
            return

        try:
            sip_headers = dict(info.all_headers or {})
            workflow_run = await db_client.create_workflow_run(
                workflow_run_name,
                target.workflow_id,
                WorkflowRunMode.SIP.value,
                user_id=target.user_id,
                call_type=CallType.INBOUND,
                initial_context={
                    "provider": WorkflowRunMode.SIP.value,
                    "agent_uuid": target.workflow_uuid,
                    "workflow_uuid": target.workflow_uuid,
                    "caller_number": info.caller_number,
                    "called_number": info.callee_number,
                    "direction": "inbound",
                    "sip_call_id": info.sip_call_id,
                    "sip": sip_headers,
                    "sip_headers": sip_headers,
                },
                gathered_context={"call_id": info.sip_call_id},
                logs={
                    "sip_invite": {
                        "from_uri": info.from_uri,
                        "to_uri": info.to_uri,
                        "codec": info.codec,
                        "sample_rate": info.sample_rate,
                        "rtp_local_port": info.rtp_local_port,
                        "rtp_remote_addr": list(info.rtp_remote_addr),
                    }
                },
                use_draft=getattr(target, "use_draft", False),
                organization_id=target.organization_id,
            )

            set_current_run_id(workflow_run.id)
            set_current_org_id(target.organization_id)
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                state=WorkflowRunState.RUNNING.value,
            )
            sip_test_session_id = getattr(target, "sip_test_session_id", None)
            if sip_test_session_id:
                sender = get_sip_test_sender(sip_test_session_id)
                if sender:
                    register_ws_sender(workflow_run.id, sender)
                    await sender(
                        {
                            "type": "sip-test-run-started",
                            "payload": {
                                "workflow_run_id": workflow_run.id,
                                "sip_call_id": info.sip_call_id,
                            },
                        }
                    )
        except Exception as e:
            logger.error(
                "Failed to create SIP workflow run for call {}: {}",
                info.sip_call_id,
                e,
                exc_info=True,
            )
            self._preflight.pop(info.sip_call_id, None)
            await dialog.hangup(reason_code=500)
            return

        audio_config = AudioConfig(
            transport_in_sample_rate=info.sample_rate,
            transport_out_sample_rate=info.sample_rate,
            vad_sample_rate=info.sample_rate,
            pipeline_sample_rate=info.sample_rate,
        )
        transport = SIPTransport(
            rtp_session=dialog.rtp_session,
            sample_rate=info.sample_rate,
        )
        task = asyncio.create_task(
            self._run_session(
                transport=transport,
                workflow_id=target.workflow_id,
                workflow_run_id=workflow_run.id,
                user_id=target.user_id,
                audio_config=audio_config,
                call_id=info.sip_call_id,
            ),
            name=f"sip_pipeline_{workflow_run.id}",
        )

        async with self._lock:
            self._sessions[info.sip_call_id] = _ActiveSession(
                call_id=info.sip_call_id,
                workflow_run_id=workflow_run.id,
                transport=transport,
                task=task,
                sip_test_session_id=getattr(target, "sip_test_session_id", None),
            )

        logger.info(
            "SIP call {} started workflow_run={} agent_uuid={}",
            info.sip_call_id,
            workflow_run.id,
            target.workflow_uuid,
        )

    async def _run_session(
        self,
        *,
        transport: SIPTransport,
        workflow_id: int,
        workflow_run_id: int,
        user_id: int,
        audio_config: AudioConfig,
        call_id: str,
    ) -> None:
        try:
            from api.services.pipecat.run_pipeline import _run_pipeline

            await _run_pipeline(
                transport,
                workflow_id,
                workflow_run_id,
                user_id,
                audio_config=audio_config,
            )
        except asyncio.CancelledError:
            logger.info("SIP pipeline cancelled for call {}", call_id)
        except Exception as e:
            logger.error("SIP pipeline failed for call {}: {}", call_id, e, exc_info=True)
        finally:
            unregister_ws_sender(workflow_run_id)
            self._ending_calls.add(call_id)
            dialog = self._server.get_dialog(call_id)
            if dialog:
                await dialog.hangup()
            await transport.close()
            async with self._lock:
                self._sessions.pop(call_id, None)
                self._preflight.pop(call_id, None)
                self._ending_calls.discard(call_id)
            logger.info("SIP call {} pipeline ended", call_id)

    async def _on_call_terminated(self, call_id: str) -> None:
        if call_id in self._ending_calls:
            return
        session = self._sessions.get(call_id)
        self._preflight.pop(call_id, None)
        if session:
            await self._stop_session(session)

    async def _stop_session(self, session: _ActiveSession) -> None:
        if not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass


async def build_sip_ingress_manager() -> Optional[SIPIngressManager]:
    """Build a SIPIngressManager from TVox telephony configs or env-var fallbacks.

    Reads host/port/RTP-range from environment variables (server infrastructure).
    Auth credentials are resolved from the first org's TVox telephony configuration,
    falling back to the ``SIP_AUTH_USERNAME`` / ``SIP_AUTH_PASSWORD`` env vars.

    Returns ``None`` when ``SIP_PORT`` is not set and no TVox config exists.
    """
    import os

    host = os.getenv("SIP_HOST", "0.0.0.0")
    port_str = os.getenv("SIP_PORT", "")
    rtp_start = int(os.getenv("SIP_RTP_START_PORT", "10000"))
    rtp_end = int(os.getenv("SIP_RTP_END_PORT", "10400"))
    max_calls = int(os.getenv("SIP_MAX_CONCURRENT_CALLS", "100"))

    port = int(port_str) if port_str else 0

    # Try to load auth from the first TVox (sip provider) config across all orgs
    auth_username = os.getenv("SIP_AUTH_USERNAME", "")
    auth_password = os.getenv("SIP_AUTH_PASSWORD", "")

    try:
        tvox_config = await db_client.get_first_tvox_config()
        if tvox_config:
            creds: Dict[str, Any] = tvox_config.get("credentials", {}) or {}
            if creds.get("auth_username"):
                auth_username = str(creds["auth_username"])
            if creds.get("auth_password"):
                auth_password = str(creds["auth_password"])
            if not port and creds.get("port"):
                port = int(creds["port"])
            logger.info(
                "SIP ingress loaded auth from TVox telephony config "
                "(organization_id={})",
                tvox_config.get("organization_id"),
            )
    except Exception as e:
        logger.warning("Could not load TVox config for SIP auth: {}", e)

    if not port:
        logger.debug("SIP_PORT not configured; SIP ingress will not start")
        return None

    return SIPIngressManager(
        host=host,
        port=port,
        rtp_start_port=rtp_start,
        rtp_end_port=rtp_end,
        max_concurrent_calls=max_calls,
        auth_username=auth_username,
        auth_password=auth_password,
    )
