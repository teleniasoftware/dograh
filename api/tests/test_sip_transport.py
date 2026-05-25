import asyncio

from api.services.sip.transport import SIPTransport


class _FakeRTPSession:
    def __init__(self):
        self.on_audio = None
        self.on_dtmf = None
        self.stopped = False

    async def stop(self):
        self.stopped = True


def test_sip_transport_registers_and_emits_client_lifecycle_events():
    async def run():
        transport = SIPTransport(rtp_session=_FakeRTPSession(), sample_rate=8000)
        events = []

        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _participant):
            events.append("connected")

        @transport.event_handler("on_client_disconnected")
        async def _on_disconnected(_transport, _participant):
            events.append("disconnected")

        assert "on_client_connected" in transport._event_handlers
        assert "on_client_disconnected" in transport._event_handlers

        await transport._emit_client_connected()
        await transport._emit_client_connected()
        await transport.close()
        await transport.close()
        await transport.cleanup()

        assert events == ["connected", "disconnected"]

    asyncio.run(run())
