"""TVox (Native SIP) transport factory.

TVox does not use the standard WebSocket transport path — SIP calls are
handled by the SIPIngressManager which creates SIPTransport instances
directly from the UDP/RTP layer.

This module exists to satisfy the ProviderSpec.transport_factory contract.
"""

from typing import Any


async def create_transport(**kwargs: Any) -> Any:
    """Not used for TVox — SIPIngressManager creates transports directly."""
    raise NotImplementedError(
        "TVox uses SIPTransport via SIPIngressManager, not the standard "
        "WebSocket transport factory path."
    )
