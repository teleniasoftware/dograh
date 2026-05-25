"""TVox (Native SIP) telephony provider package.

TVox enables inbound SIP calls to Dograh agents.
Dial ``sip:<agent_uuid>@<host>:<port>`` from any SIP client.
"""

from typing import Any, Dict

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import SIPConfigurationRequest, SIPConfigurationResponse
from .provider import SIPProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": "sip",
        "host": value.get("host", "0.0.0.0"),
        "port": int(value.get("port", 5090)),
        "rtp_start_port": int(value.get("rtp_start_port", 10000)),
        "rtp_end_port": int(value.get("rtp_end_port", 10400)),
        "max_concurrent_calls": int(value.get("max_concurrent_calls", 100)),
        "auth_username": value.get("auth_username", ""),
        "auth_password": value.get("auth_password", ""),
    }


_UI_METADATA = ProviderUIMetadata(
    display_name="TVox",
    docs_url="https://docs.dograh.com/integrations/telephony/tvox",
    fields=[
        ProviderUIField(
            name="host",
            label="SIP Host",
            type="text",
            description="SIP server bind address (default: 0.0.0.0)",
        ),
        ProviderUIField(
            name="port",
            label="SIP Port",
            type="number",
            description="SIP server UDP port (default: 5090)",
        ),
        ProviderUIField(
            name="rtp_start_port",
            label="RTP Start Port",
            type="number",
            description="Start of RTP port range (default: 10000)",
        ),
        ProviderUIField(
            name="rtp_end_port",
            label="RTP End Port",
            type="number",
            description="End of RTP port range (default: 10400)",
        ),
        ProviderUIField(
            name="max_concurrent_calls",
            label="Max Concurrent Calls",
            type="number",
            description="Maximum concurrent SIP calls (default: 100)",
        ),
        ProviderUIField(
            name="auth_username",
            label="Auth Username",
            type="text",
            description="SIP authentication username (optional)",
        ),
        ProviderUIField(
            name="auth_password",
            label="Auth Password",
            type="password",
            sensitive=True,
            description="SIP authentication password (optional)",
        ),
    ],
)


SPEC = ProviderSpec(
    name="sip",
    provider_cls=SIPProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    transport_sample_rate=8000,
    config_request_cls=SIPConfigurationRequest,
    ui_metadata=_UI_METADATA,
    config_response_cls=SIPConfigurationResponse,
    account_id_credential_field="",  # TVox has no account-id concept
)


register(SPEC)


__all__ = [
    "SPEC",
    "SIPConfigurationRequest",
    "SIPConfigurationResponse",
    "SIPProvider",
    "create_transport",
]
