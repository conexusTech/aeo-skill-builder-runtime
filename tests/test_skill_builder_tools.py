"""Tool-call emit + gateway-result handling (PRD §8)."""

import pytest

from app.skill_builder import draft, tools
from app.skill_builder.protocol.agui import AGUIEmitter, EventType
from app.skill_builder.tools import (
    RejectionKind,
    ToolName,
    ToolResult,
    ToolResultStatus,
)


def _complete_config():
    return draft.skeleton(
        name="ACME Prospect Scanner",
        vertical="auto parts",
        lead_type="B",  # organizations.lead_type ENUM (A / B / MIXED)
        product_description="Prospect scanner for the auto parts vertical.",
        type_="customer",
    )


def _types(em):
    return [e.type for e in em.events]


# --- emit ------------------------------------------------------------------


def test_request_test_run_emits_tool_call_when_config_complete():
    em = AGUIEmitter()
    outcome = tools.request_test_run(em, _complete_config(), notes="smoke")
    assert outcome.requested is True
    assert outcome.tool_call_id is not None
    assert _types(em) == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]
    assert em.wire_events()[0]["toolCallName"] == "request_test_run"


def test_request_test_run_blocks_on_incomplete_config():
    em = AGUIEmitter()
    outcome = tools.request_test_run(em, {"geography": {}})  # no catalog metadata
    assert outcome.requested is False
    assert outcome.issues  # complete-mode validation caught the gaps
    # Surfaced conversationally + interrupt, NO tool call emitted.
    assert EventType.TOOL_CALL_START not in _types(em)
    assert _types(em)[-1] == EventType.RUN_FINISHED


def test_request_finalize_emits_with_slug():
    em = AGUIEmitter()
    outcome = tools.request_finalize(em, _complete_config(), slug="auto-parts-prospect-scanner")
    assert outcome.requested is True
    import json
    args = json.loads(em.wire_events()[1]["delta"])
    assert args["slug"] == "auto-parts-prospect-scanner"


def test_request_finalize_blocks_on_bad_slug_arg():
    em = AGUIEmitter()
    outcome = tools.request_finalize(em, _complete_config(), slug="Not A Slug")
    assert outcome.requested is False
    assert any("slug" in i.location or "slug" in i.message for i in outcome.issues)
    assert EventType.TOOL_CALL_START not in _types(em)


# --- parse -----------------------------------------------------------------


def test_parse_tool_result_from_dict():
    res = tools.parse_tool_result(
        {"tool_name": "request_test_run", "status": "succeeded", "summary": {"prospects": 3}}
    )
    assert res.tool_name == ToolName.REQUEST_TEST_RUN
    assert res.status == ToolResultStatus.SUCCEEDED


def test_parse_tool_result_from_json_string():
    res = tools.parse_tool_result('{"tool_name": "request_finalize", "status": "succeeded"}')
    assert res.tool_name == ToolName.REQUEST_FINALIZE


def test_parse_tool_result_rejects_non_json_string():
    with pytest.raises(ValueError):
        tools.parse_tool_result("not json")


def test_parse_tool_result_rejects_missing_status():
    with pytest.raises(ValueError):
        tools.parse_tool_result({"tool_name": "request_test_run"})


# --- handle results --------------------------------------------------------


def test_rejection_is_not_terminal_and_routes_to_repair():
    em = AGUIEmitter()
    result = ToolResult(
        tool_name=ToolName.REQUEST_FINALIZE,
        status=ToolResultStatus.REJECTED,
        issues=[{"location": "/geography", "message": "literal, expected context key",
                 "kind": RejectionKind.ORG_COUPLING}],
    )
    tools.handle_tool_result(em, result)
    finished = em.wire_events()[-1]
    assert finished["type"] == EventType.RUN_FINISHED
    assert finished["result"]["outcome"] == "interrupt"
    assert finished["result"]["reason"] == "awaiting_phase_acceptance"
    # The rejection detail is surfaced to the operator.
    assert "org_coupling" in em.wire_events()[1]["delta"]


def test_declined_test_run_surfaces_explanation():
    em = AGUIEmitter()
    result = ToolResult(
        tool_name=ToolName.REQUEST_TEST_RUN,
        status=ToolResultStatus.DECLINED,
        explanation="Real test runs are disabled for this tenant.",
    )
    tools.handle_tool_result(em, result)
    assert "disabled for this tenant" in em.wire_events()[1]["delta"]
    assert em.wire_events()[-1]["result"]["reason"] == "awaiting_decision"


def test_successful_test_run_offers_review():
    em = AGUIEmitter()
    result = ToolResult(
        tool_name=ToolName.REQUEST_TEST_RUN,
        status=ToolResultStatus.SUCCEEDED,
        summary={"scored_prospects": 5},
    )
    tools.handle_tool_result(em, result)
    assert em.wire_events()[-1]["result"]["step"] == "review_test_results"


def test_successful_finalize_is_terminal():
    em = AGUIEmitter()
    result = ToolResult(tool_name=ToolName.REQUEST_FINALIZE, status=ToolResultStatus.SUCCEEDED)
    tools.handle_tool_result(em, result)
    finished = em.wire_events()[-1]
    assert finished["type"] == EventType.RUN_FINISHED
    # finalize success is an outcome, NOT an interrupt awaiting the human.
    assert finished["result"]["outcome"] == "finalized"
    assert "SU" in em.wire_events()[1]["delta"] or "super-user" in em.wire_events()[1]["delta"]


# --- forward compatibility + the status claim (2026-08-04) ------------------


def test_an_unknown_rejection_kind_degrades_to_other_instead_of_killing_the_turn():
    """A fourth rejection kind must not break every rejection.

    `other` exists to mean "anything else", and we told the gateway it is the
    default when a kind is absent — but an unknown VALUE was a hard
    ValidationError, which `parse_tool_result` turns into a RUN_ERROR. So adding a
    kind on their side would have destroyed the operator's repair path at exactly
    the moment they needed it, rather than degrading. Safe to coerce because
    nothing branches on `kind`; it is rendered into the repair message.
    """
    result = tools.parse_tool_result({
        "tool_name": "request_finalize",
        "status": "rejected",
        "issues": [
            {"location": "geography.home_markets", "message": "nope",
             "kind": "a_kind_invented_next_quarter"},
            {"location": "/", "message": "absent kind"},
            {"location": "/x", "message": "known kind", "kind": "org_coupling"},
        ],
    })
    assert [i.kind for i in result.issues] == [
        tools.RejectionKind.OTHER,
        tools.RejectionKind.OTHER,
        tools.RejectionKind.ORG_COUPLING,
    ]
    # And the turn still routes to repair rather than erroring out.
    emitter = AGUIEmitter(thread_id="t")
    tools.handle_tool_result(emitter, result)
    assert EventType.RUN_ERROR not in [e.type for e in emitter.events]


def test_an_unknown_status_still_fails_loudly_and_that_asymmetry_is_deliberate():
    """`status` drives behaviour, so there is no honest generalisation for it.

    Coercing an unknown status would either repair a turn that finished or end one
    that needed repair. Asserted so the asymmetry with `kind` reads as a decision
    rather than an inconsistency someone should "fix".
    """
    with pytest.raises(ValueError, match="unrecognized tool result shape"):
        tools.parse_tool_result({"tool_name": "request_finalize", "status": "queued"})


def test_the_finalize_message_promises_no_status():
    """It used to say the skill was created with status 'tested'.

    The gateway sets `tested` only when a real test run backs the session and
    `configured` otherwise, and R6 is blocked — so every finalize today lands
    `configured` and that sentence was false for every real outcome. Frontend
    shipped it in two places and backend's tool description in a third.
    """
    emitter = AGUIEmitter(thread_id="t")
    tools.handle_tool_result(emitter, tools.parse_tool_result({
        "tool_name": "request_finalize", "status": "succeeded",
    }))
    text = " ".join(
        e["delta"] for e in emitter.wire_events()
        if e["type"] == EventType.TEXT_MESSAGE_CONTENT
    )
    assert "tested" not in text
    assert "configured" not in text
    # The facts we do know are still reported.
    assert "connected" in text
    assert "activation" in text
