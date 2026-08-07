"""AG-UI protocol module — inbound parse + outbound event shapes + emitter.

These pin the exact wire shapes we emit (contract #2 we owe the gateway) and
prove the runtime is testable against a mocked stream (PRD §15) — no AWS/SSE
transport needed, just assertions over the emitter's event list.
"""

import json
from itertools import count

from app.skill_builder.protocol.agui import (
    AGUIEmitter,
    EventType,
    InterruptReason,
    RunAgentInput,
    encode_sse,
)


def _ids():
    c = count(1)
    return lambda: f"id-{next(c)}"


# --- inbound RunAgentInput -------------------------------------------------


def test_run_agent_input_parses_camelcase_envelope():
    payload = {
        "threadId": "sess-9",
        "runId": "run-9",
        "messages": [{"role": "user", "content": "hi"}],
        "state": {"draftConfig": {"name": "x"}, "acceptance": {"geography": True}},
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }
    ri = RunAgentInput.model_validate(payload)
    assert ri.thread_id == "sess-9"
    assert ri.run_id == "run-9"
    assert ri.state.draft_config == {"name": "x"}
    assert ri.state.is_phase_accepted("geography")
    assert ri.forwarded_props.customer_context == {"organization_name": "ACME"}


def test_is_kickoff_true_when_no_assistant_message():
    ri = RunAgentInput.model_validate({"messages": [{"role": "user", "content": "go"}]})
    assert ri.is_kickoff is True


def test_is_kickoff_false_once_assistant_has_spoken():
    ri = RunAgentInput.model_validate(
        {"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "next"},
        ]}
    )
    assert ri.is_kickoff is False
    assert ri.last_user_text() == "next"


def test_empty_payload_defaults_are_safe():
    ri = RunAgentInput.model_validate({})
    assert ri.messages == []
    assert ri.state.draft_config == {}
    assert ri.forwarded_props.customer_context == {}
    assert ri.is_kickoff is True


def test_unknown_envelope_fields_ignored():
    ri = RunAgentInput.model_validate({"messages": [], "tools": [{"x": 1}], "context": []})
    assert ri.messages == []


# --- outbound event shapes -------------------------------------------------


def test_text_message_events_serialize_camelcase():
    em = AGUIEmitter(thread_id="t", run_id="r", id_factory=_ids())
    mid = em.message("hello")
    assert mid == "id-1"
    wire = em.wire_events()
    assert [e["type"] for e in wire] == [
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
    ]
    assert wire[0]["messageId"] == "id-1"
    assert wire[0]["role"] == "assistant"
    assert wire[1]["delta"] == "hello"
    assert wire[2]["messageId"] == "id-1"


def test_state_delta_carries_rfc6902_ops():
    em = AGUIEmitter()
    em.state_delta([{"op": "add", "path": "/geography", "value": {}}])
    wire = em.wire_events()[0]
    assert wire["type"] == EventType.STATE_DELTA
    assert wire["delta"][0]["op"] == "add"


def test_run_finished_interrupt_outcome():
    em = AGUIEmitter(thread_id="t", run_id="r")
    em.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase="geography")
    wire = em.wire_events()[0]
    assert wire["type"] == EventType.RUN_FINISHED
    assert wire["result"] == {
        "outcome": "interrupt",
        "reason": "awaiting_phase_acceptance",
        "phase": "geography",
    }
    assert wire["threadId"] == "t"


def test_run_error_shape():
    em = AGUIEmitter()
    em.run_error("boom", code="internal_error")
    wire = em.wire_events()[0]
    assert wire == {"type": EventType.RUN_ERROR, "message": "boom", "code": "internal_error"}


def test_tool_call_start_args_end_sequence():
    em = AGUIEmitter(id_factory=_ids())
    tcid = em.tool_call("request_test_run", {"draft_config": {"name": "x"}})
    assert tcid == "id-1"
    wire = em.wire_events()
    assert [e["type"] for e in wire] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]
    assert wire[0]["toolCallName"] == "request_test_run"
    assert wire[0]["toolCallId"] == "id-1"
    assert json.loads(wire[1]["delta"]) == {"draft_config": {"name": "x"}}


def test_encode_sse_frame_format():
    em = AGUIEmitter()
    em.run_error("x")
    frame = encode_sse(em.events[0])
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    body = json.loads(frame[len("data: "):].strip())
    assert body["type"] == EventType.RUN_ERROR


def test_none_fields_excluded_from_wire():
    # RunFinished with no result must not emit a null "result" key.
    em = AGUIEmitter()
    em.run_finished()
    assert "result" not in em.wire_events()[0]
