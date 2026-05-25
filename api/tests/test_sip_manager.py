import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from api.enums import WorkflowRunMode
from api.services.sip.call_info import SIPCallInfo
from api.services.sip.manager import SIPIngressManager


def _workflow(**overrides):
    data = {
        "id": 33,
        "workflow_uuid": "agent-uuid-123",
        "organization_id": 11,
        "user_id": 99,
        "status": "active",
        "released_definition": SimpleNamespace(id=77),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_pre_call_accepts_active_agent_uuid_and_caches_target():
    async def run():
        manager = SIPIngressManager()
        quota = SimpleNamespace(has_quota=True, error_message="")
        workflow = _workflow()

        with (
            patch("api.services.sip.manager.db_client") as mock_db,
            patch(
                "api.services.sip.manager.check_dograh_quota_by_user_id",
                new=AsyncMock(return_value=quota),
            ) as quota_mock,
        ):
            mock_db.get_workflow_by_uuid_unscoped = AsyncMock(return_value=workflow)

            rejection = await manager._on_pre_call(
                "agent-uuid-123",
                "sip-call-1",
                {},
            )

        assert rejection is None
        assert manager._preflight["sip-call-1"].workflow_uuid == workflow.workflow_uuid
        quota_mock.assert_awaited_once_with(workflow.user_id, workflow_id=workflow.id)

    asyncio.run(run())


def test_pre_call_rejects_missing_agent_uuid():
    async def run():
        manager = SIPIngressManager()

        with patch("api.services.sip.manager.db_client") as mock_db:
            mock_db.get_workflow_by_uuid_unscoped = AsyncMock(return_value=None)

            rejection = await manager._on_pre_call("missing", "sip-call-1", {})

        assert rejection == 404
        assert "sip-call-1" not in manager._preflight

    asyncio.run(run())


def test_pre_call_rejects_quota_exhausted():
    async def run():
        manager = SIPIngressManager()
        workflow = _workflow()
        quota = SimpleNamespace(has_quota=False, error_message="quota exceeded")

        with (
            patch("api.services.sip.manager.db_client") as mock_db,
            patch(
                "api.services.sip.manager.check_dograh_quota_by_user_id",
                new=AsyncMock(return_value=quota),
            ),
        ):
            mock_db.get_workflow_by_uuid_unscoped = AsyncMock(return_value=workflow)

            rejection = await manager._on_pre_call(
                workflow.workflow_uuid,
                "sip-call-1",
                {},
            )

        assert rejection == 402
        assert "sip-call-1" not in manager._preflight

    asyncio.run(run())


def test_established_call_creates_inbound_sip_run():
    async def run():
        manager = SIPIngressManager()
        workflow = _workflow()
        manager._preflight["sip-call-1"] = SimpleNamespace(
            workflow_id=workflow.id,
            workflow_uuid=workflow.workflow_uuid,
            organization_id=workflow.organization_id,
            user_id=workflow.user_id,
        )
        manager._server.get_dialog = Mock(
            return_value=SimpleNamespace(rtp_session=object())
        )
        manager._run_session = AsyncMock()
        workflow_run = SimpleNamespace(id=501)
        info = SIPCallInfo(
            session_id=1,
            call_uuid="sip-call-1",
            sip_call_id="sip-call-1",
            from_uri="sip:+39123@host",
            to_uri=f"sip:{workflow.workflow_uuid}@host",
            caller_number="+39123",
            callee_number=workflow.workflow_uuid,
            agent_id=workflow.workflow_uuid,
            codec="PCMA",
            sample_rate=8000,
            rtp_local_port=12000,
            rtp_remote_addr=("10.0.0.1", 10000),
            all_headers={"x-test": "yes"},
        )

        with patch("api.services.sip.manager.db_client") as mock_db:
            mock_db.create_workflow_run = AsyncMock(return_value=workflow_run)
            mock_db.update_workflow_run = AsyncMock()

            await manager._on_call_established(info)

        create_args = mock_db.create_workflow_run.await_args.args
        create_kwargs = mock_db.create_workflow_run.await_args.kwargs
        assert create_args[2] == WorkflowRunMode.SIP.value
        assert create_kwargs["call_type"].value == "inbound"
        assert create_kwargs["initial_context"]["agent_uuid"] == workflow.workflow_uuid
        assert create_kwargs["initial_context"]["sip_call_id"] == "sip-call-1"
        assert create_kwargs["gathered_context"]["call_id"] == "sip-call-1"

    asyncio.run(run())
