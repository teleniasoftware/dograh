import socket
from unittest.mock import patch

import aiohttp
import pytest

from api.utils.tunnel import TunnelURLProvider


class TestTunnelURLProvider:
    @pytest.mark.asyncio
    async def test_missing_cloudflared_host_raises_without_warning(self):
        connection_key = object()
        dns_error = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        client_error = aiohttp.ClientConnectorError(connection_key, dns_error)

        with patch("api.utils.tunnel.logger") as mock_logger:
            with patch.object(
                TunnelURLProvider,
                "_get_cloudflared_urls",
                side_effect=client_error,
            ):
                with pytest.raises(ValueError, match="No tunnel URL available"):
                    await TunnelURLProvider.get_tunnel_urls()

        mock_logger.warning.assert_not_called()
        mock_logger.debug.assert_called_once()
