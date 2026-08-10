"""BedrockChatModel.decide against a stubbed provider response.

There were NO tests for this class until #27. That is not incidental — it is
why `output_config.format` (rejected by Mantle) and then the empty-response
crash both reached a live runtime. The whole suite runs on FakeChatModel, so a
green run says nothing about the class that actually talks to Bedrock.

These stub the provider response instead of the network: the decode path is
ours and is what keeps breaking.
"""

import pytest

from app.skill_builder.model import BedrockChatModel, _DECISION_TOOL


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Usage:
    input_tokens = 900
    output_tokens = 16000
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Client:
    """Captures the request and returns a scripted response."""

    def __init__(self, response):
        self._response = response
        self.captured = None
        self.messages = self

    def create(self, **kw):
        self.captured = kw
        return self._response


class _Prompt:
    def split(self, stable_layers=5):
        return "stable", "volatile"


def _model(response):
    m = BedrockChatModel(model_id="anthropic.claude-sonnet-5", aws_region="us-east-1")
    m._client = _Client(response)
    return m


def _decide(m):
    return m.decide(prompt=_Prompt(), messages=[], draft_config={}, open_phase="geography")


def test_thinking_only_response_names_the_real_cause():
    """#27 turn 4: a thinking block, no tool_use, no text, stop_reason max_tokens.

    This used to reach json.loads("") and surface as a JSONDecodeError pointing
    at the parser — backend had to dig through our CloudWatch to find that the
    budget, not the parser, was the problem. The error must name stop_reason
    and the budget.
    """
    m = _model(_Response([_Block("thinking", thinking="...")], stop_reason="max_tokens"))

    with pytest.raises(RuntimeError) as exc:
        _decide(m)

    msg = str(exc.value)
    assert "max_tokens" in msg, "must name the stop reason"
    assert "thinking" in msg, "must say what blocks DID come back"
    assert "raise max_tokens or lower effort" in msg, "must say what to do"


def test_decision_is_read_from_the_tool_call():
    m = _model(
        _Response([
            _Block("thinking", thinking="..."),
            _Block("tool_use", name="emit_decision",
                   input={"action": "propose_section", "message": "here",
                          "phase": "geography", "section_json": '{"home_markets": {}}'}),
        ])
    )
    d = _decide(m)
    assert d.action == "propose_section"
    assert d.section == {"home_markets": {}}, "section_json must be decoded"


def test_request_declares_the_tool_and_does_not_force_it():
    """Forcing tool_choice on Mantle suppresses thinking and measurably
    degrades the answer (see the constant's comment). Pin that we do not."""
    m = _model(
        _Response([_Block("tool_use", name="emit_decision",
                          input={"action": "await_human", "message": "ok"})])
    )
    _decide(m)

    sent = m._client.captured
    assert sent["tools"] == [_DECISION_TOOL]
    assert "tool_choice" not in sent, "must stay unforced"
    assert "output_config" not in sent, "Mantle rejects output_config.format"
    assert sent["thinking"] == {"type": "adaptive"}


def test_handle_turn_survives_an_empty_model_response():
    """#27's explicit ask: drive `handle_turn`, not the parser.

    An isolated parse test passes on well-formed text, and a `decide()` test
    still stops short of the turn handler. This drives the real handler with a
    real BedrockChatModel whose only fake is the provider response — the seam
    where #27 actually broke — and asserts the turn degrades into an in-stream
    RUN_ERROR rather than escaping as a 500.
    """
    from app.skill_builder.protocol.agui import EventType
    from app.skill_builder.runtime import handle_turn

    m = _model(_Response([_Block("thinking", thinking="...")], stop_reason="max_tokens"))

    res = handle_turn(
        {
            "threadId": "t-27",
            "messages": [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "proposal"},
                {"role": "user", "content": "ok"},
            ],
            "state": {"draftConfig": {}, "acceptance": {"geography": True}},
            "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
        },
        model=m,
    )

    types = [e.type for e in res.emitter.events]
    assert EventType.RUN_ERROR in types, "must surface in-stream, not crash the invocation"
    assert types[0] == EventType.RUN_STARTED, "stream must still open cleanly"


def test_prose_answer_retries_once_with_the_tool_forced():
    """Frontend's first E2E died here (2026-08-10).

    The model answered in prose with no tool call. The old guard only caught
    EMPTY text, and `json.loads("Some prose")` raises the same
    JSONDecodeError at char 0 as `json.loads("")` — identical in a stack
    trace, different cause. Now: retry once with the tool forced, because the
    unforced call already failed and a worse answer beats no answer.
    """
    calls = []

    class _TwoShot(_Client):
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return _Response([_Block("text", text="Sure, running that now!")])
            return _Response([
                _Block("tool_use", name="emit_decision",
                       input={"action": "await_human", "message": "ok"}),
            ])

    m = BedrockChatModel(model_id="anthropic.claude-sonnet-5", aws_region="us-east-1")
    m._client = _TwoShot(None)

    d = _decide(m)

    assert d.action == "await_human", "the retry's decision must be used"
    assert len(calls) == 2, "exactly one retry — never a loop"
    assert "tool_choice" not in calls[0], "first call stays unforced"
    assert calls[1]["tool_choice"] == {"type": "tool", "name": "emit_decision"}


def test_the_retry_sums_usage_instead_of_dropping_the_first_call():
    """Two model calls really were paid for. Reporting only the retry
    under-bills every repaired turn, and it would look correct doing it (#14)."""
    class _TwoShot(_Client):
        def create(self, **kw):
            if not hasattr(self, "_n"):
                self._n = 0
            self._n += 1
            if self._n == 1:
                return _Response([_Block("text", text="prose")])
            return _Response([
                _Block("tool_use", name="emit_decision",
                       input={"action": "await_human", "message": "ok"}),
            ])

    m = BedrockChatModel(model_id="anthropic.claude-sonnet-5", aws_region="us-east-1")
    m._client = _TwoShot(None)

    d = _decide(m)
    # _Usage reports 16000 output per call; both calls must be counted.
    assert d.usage.output_tokens == 32000, "first call's tokens were dropped"


def test_a_thinking_only_response_does_NOT_retry():
    """No text means the model produced nothing — usually max_tokens eaten by
    thinking. A retry would burn the budget again and fail identically, so it
    must fail fast with the descriptive error instead."""
    calls = []

    class _Once(_Client):
        def create(self, **kw):
            calls.append(kw)
            return _Response([_Block("thinking", thinking="...")],
                             stop_reason="max_tokens")

    m = BedrockChatModel(model_id="anthropic.claude-sonnet-5", aws_region="us-east-1")
    m._client = _Once(None)

    with pytest.raises(RuntimeError, match="no decision"):
        _decide(m)
    assert len(calls) == 1, "must not retry when there was no text to begin with"


def test_prose_surviving_the_forced_retry_names_the_cause():
    """If even the forced retry answers in prose, the error must say so rather
    than pointing at json.loads — that opacity cost a cross-repo dig."""
    class _AlwaysProse(_Client):
        def create(self, **kw):
            return _Response([_Block("text", text="I'd rather just explain it.")])

    m = BedrockChatModel(model_id="anthropic.claude-sonnet-5", aws_region="us-east-1")
    m._client = _AlwaysProse(None)

    with pytest.raises(RuntimeError, match="answered in prose"):
        _decide(m)
