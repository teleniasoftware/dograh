from api.services.pipecat.event_handlers import _get_turn_trace_id


def test_get_turn_trace_id_uses_public_method_when_available():
    class Observer:
        def get_trace_id(self):
            return "abc123"

    assert _get_turn_trace_id(Observer()) == "abc123"


def test_get_turn_trace_id_reads_conversation_span_context():
    class SpanContext:
        trace_id = int("0123456789abcdef0123456789abcdef", 16)

    class ConversationSpan:
        def get_span_context(self):
            return SpanContext()

    class Observer:
        _conversation_span = ConversationSpan()

    assert (
        _get_turn_trace_id(Observer())
        == "0123456789abcdef0123456789abcdef"
    )


def test_get_turn_trace_id_returns_none_without_active_span():
    class Observer:
        pass

    assert _get_turn_trace_id(Observer()) is None
