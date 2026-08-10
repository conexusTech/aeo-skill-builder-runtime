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
