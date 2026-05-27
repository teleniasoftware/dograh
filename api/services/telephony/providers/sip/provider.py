"""TVox (Native SIP) telephony provider.

TVox is an inbound-only provider — it does not initiate outbound calls.
Instead, the SIP server listens for incoming INVITEs and maps them to
Dograh workflow runs.

Configuration is stored per-organization in telephony_configurations.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from api.enums import WorkflowRunMode
from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    TelephonyProvider,
)

if TYPE_CHECKING:
    from fastapi import WebSocket


class SIPProvider(TelephonyProvider):
    """TVox native SIP ingress provider.

    Inbound-only. The SIP UDP server (SIPIngressManager) handles call
    setup; this class exists to satisfy the TelephonyProvider interface
    and to store org-scoped configuration.
    """

    PROVIDER_NAME = WorkflowRunMode.SIP.value
    WEBHOOK_ENDPOINT = None  # SIP uses UDP, not HTTP webhooks

    def __init__(self, config: Dict[str, Any]):
        self._host = config.get("host", "0.0.0.0")
        self._port = int(config.get("port", 5090))
        self._rtp_start_port = int(config.get("rtp_start_port", 10000))
        self._rtp_end_port = int(config.get("rtp_end_port", 10400))
        self._max_concurrent_calls = int(config.get("max_concurrent_calls", 100))
        self._auth_username = config.get("auth_username", "")
        self._auth_password = config.get("auth_password", "")
        self._callback_url = config.get("callback_url")
        self._callback_credential_uuid = config.get("callback_credential_uuid")

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def rtp_start_port(self) -> int:
        return self._rtp_start_port

    @property
    def rtp_end_port(self) -> int:
        return self._rtp_end_port

    @property
    def max_concurrent_calls(self) -> int:
        return self._max_concurrent_calls

    @property
    def auth_username(self) -> str:
        return self._auth_username

    @property
    def auth_password(self) -> str:
        return self._auth_password

    @property
    def callback_url(self) -> Optional[str]:
        return self._callback_url

    @property
    def callback_credential_uuid(self) -> Optional[str]:
        return self._callback_credential_uuid

    # ── TelephonyProvider interface ──────────────────────────────────

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        raise NotImplementedError("TVox is inbound-only. Use /api/v1/telephony/initiate-call with a different provider.")

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        raise NotImplementedError("TVox is inbound-only.")

    async def get_available_phone_numbers(self) -> List[str]:
        return []

    def validate_config(self) -> bool:
        return True  # Minimal validation; SIP server validates at runtime

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        return True

    async def get_webhook_response(
        self, workflow_id: int, user_id: int, workflow_run_id: int
    ) -> str:
        return ""

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        return {"cost_usd": 0.0, "duration": 0, "status": "unknown"}

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        user_id: int,
        workflow_run_id: int,
    ) -> None:
        raise NotImplementedError("TVox does not use WebSocket transports.")

    # ── Inbound methods ─────────────────────────────────────────────

    @classmethod
    def can_handle_webhook(
        cls, webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        return False

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        return NormalizedInboundData(
            provider=SIPProvider.PROVIDER_NAME,
            call_id="",
            from_number="",
            to_number="",
            direction="inbound",
            call_status="",
            account_id=None,
            raw_data=webhook_data,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        return True

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        return True

    async def start_inbound_stream(self, **kwargs):
        from fastapi import Response
        return Response(content="", status_code=204)

    @staticmethod
    def generate_error_response(error_type: str, message: str):
        import json
        from fastapi import Response
        return Response(
            content=json.dumps({"error": error_type, "message": message}),
            media_type="application/json",
        )

    @staticmethod
    def generate_validation_error_response(error_type):
        import json
        from fastapi import Response
        from api.errors.telephony_errors import TELEPHONY_ERROR_MESSAGES, TelephonyError
        message = TELEPHONY_ERROR_MESSAGES.get(
            error_type, TELEPHONY_ERROR_MESSAGES[TelephonyError.GENERAL_AUTH_FAILED]
        )
        return Response(
            content=json.dumps({"error": str(error_type), "message": message}),
            media_type="application/json",
        )
