"""`RUN_FINISHED.result` — contract #5, plus #14's usage carrier.

Two cross-repo asks land in the same object, so they are tested together: the
interrupt vocabulary the gateway and frontend render controls from, and the token
usage that is the ONLY path by which a builder session's cost becomes known.
"""

from app.skill_builder.model import FakeChatModel, ModelDecision, _usage_from_response
from app.skill_builder.protocol.agui import (
    EMITTED_INTERRUPT_REASONS,
    AGUIEmitter,
    EventType,
    InterruptReason,
    TokenUsage,
    coerce_interrupt_reason,
)
from app.skill_builder.runtime import handle_turn


def _terminal(emitter):
    wire = emitter.wire_events()
    assert wire[-1]["type"] == EventType.RUN_FINISHED
    return wire[-1].get("result")


def _continuation(acceptance=None, user="ok"):
    return {
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "proposal"},
            {"role": "user", "content": user},
        ],
        "state": {"draftConfig": {}, "acceptance": acceptance or {}},
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }


# --- contract #5: the interrupt vocabulary ---------------------------------


def test_the_wire_reason_is_the_snake_case_enum_value_not_the_member_repr():
    """`str(member)` being the value is a property of StrEnum, not of Enum.

    A plain `Enum` with a str mixin renders as `InterruptReason.AWAITING_DECISION`
    in 3.11+, which would put a Python repr on the wire — and this exact mistake
    (assuming `str(member)` is the value) has already cost this repo a whole
    geo-local run. Asserted rather than assumed.
    """
    emitter = AGUIEmitter(thread_id="t")
    emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase="geography")
    result = _terminal(emitter)
    assert result["reason"] == "awaiting_phase_acceptance"
    assert "InterruptReason" not in str(result)


def test_a_model_invented_reason_is_coerced_not_forwarded():
    """The one path where `reason` does not originate in our enum.

    `ModelDecision.interrupt_reason` is free text, so a model can propose
    anything. Forwarded, it reaches the operator's browser as a reason no
    consumer has a branch for — and nothing looks wrong, because the turn
    succeeded: the UI simply renders no control and the conversation appears to
    stall by itself.
    """
    for invented in ("awaiting_user_input", "waiting for the human", "", "AWAITING_DECISION"):
        assert coerce_interrupt_reason(invented) is InterruptReason.AWAITING_DECISION
    assert coerce_interrupt_reason(None) is InterruptReason.AWAITING_DECISION
    # Known values survive untouched, including as plain strings.
    for reason in InterruptReason:
        assert coerce_interrupt_reason(reason) is reason
        assert coerce_interrupt_reason(reason.value) is reason


def test_a_model_invented_reason_does_not_reach_the_wire_end_to_end():
    model = FakeChatModel(ModelDecision(
        action="await_human", message="Waiting.", interrupt_reason="please_advise",
    ))
    res = handle_turn(_continuation(), model=model)
    result = _terminal(res.emitter)
    assert result["reason"] == "awaiting_decision"
    assert "please_advise" not in str(result)


def test_emitted_reasons_exclude_the_declared_but_unreachable_one():
    """`AWAITING_FINALIZE` is declared and emitted by no path.

    Recorded as a distinct fact from "declared" because a consumer that builds a
    required branch for it is waiting on an event that cannot arrive. If a future
    path emits it, add it here — the frozenset is what the cross-repo contract
    quotes.
    """
    assert EMITTED_INTERRUPT_REASONS < set(InterruptReason)
    assert InterruptReason.AWAITING_FINALIZE not in EMITTED_INTERRUPT_REASONS
    assert EMITTED_INTERRUPT_REASONS == {
        InterruptReason.AWAITING_PHASE_ACCEPTANCE,
        InterruptReason.AWAITING_DECISION,
        InterruptReason.AWAITING_TEST_RUN,
    }


def test_interrupt_drops_none_detail_so_phase_is_genuinely_absent():
    """Frontend's reading was right: `phase` is optional, not empty-when-unknown.

    A consumer must not assume presence — and we must not emit `phase: null`,
    which reads as "no phase" to a truthiness check but as a present key to a
    schema."""
    emitter = AGUIEmitter(thread_id="t")
    emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE)
    assert "phase" not in _terminal(emitter)


# --- contract #5 conformance (root is CLOSED) ------------------------------


def test_every_terminal_path_conforms_to_contract_5():
    """Contract #5's root is `additionalProperties: false` as of v1.

    That makes this the one contract we can break by ADDING something:
    `AGUIEmitter.interrupt` forwards arbitrary `**detail`, so a new detail kwarg
    is a hard rejection at the gateway rather than a field nobody reads. Every
    result we can emit is checked, built through the emitter rather than
    hand-written, so the assertion covers what the emitter actually produces.
    """
    from app.skill_builder.validator import validate_run_finished_result

    emitters = []
    for build in (
        lambda em: em.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase="geography"),
        lambda em: em.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE),
        lambda em: em.interrupt(InterruptReason.AWAITING_TEST_RUN),
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION, step="connect_or_build",
                                match_slug="auto-parts-prospect-scanner"),
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION, step="connect",
                                match_slug="auto-parts-prospect-scanner"),
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION, step="kickoff_confirmation"),
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION, step="request_declined"),
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION, step="review_test_results"),
        lambda em: em.run_finished({"outcome": "tool_call", "tool": "request_test_run"}),
        lambda em: em.run_finished({"outcome": "tool_call", "tool": "request_finalize"}),
        lambda em: em.run_finished({"outcome": "finalized"}),
    ):
        emitter = AGUIEmitter(thread_id="t")
        emitter.record_usage(TokenUsage(input_tokens=10, cache_read_tokens=2))
        build(emitter)
        emitters.append(emitter)

    for emitter in emitters:
        result = _terminal(emitter)
        assert validate_run_finished_result(result) == [], result

    # Guards the loop: a rename that silently emitted nothing would pass above.
    assert len(emitters) == 11


def test_an_undeclared_detail_key_is_a_contract_violation():
    """The negative control, and the realistic failure mode.

    `interrupt(**detail)` accepts anything, so the way this contract breaks is a
    future caller adding a detail the gateway never declared — which under a
    closed root is rejected outright, not ignored.
    """
    from app.skill_builder.validator import validate_run_finished_result

    emitter = AGUIEmitter(thread_id="t")
    emitter.interrupt(InterruptReason.AWAITING_DECISION, step="connect", helpfully_added="x")
    issues = validate_run_finished_result(_terminal(emitter))
    assert issues, "a closed root must reject an undeclared key"
    assert any("helpfully_added" in i.message for i in issues)


def test_a_bare_terminal_stays_bare_even_with_usage_recorded():
    """Audit finding on our own change: attaching usage must not INVENT a result.

    Contract #5 declares `outcome` required on any result, so synthesising
    `{"usage": …}` onto a bare `run_finished()` emits a result the gateway rejects
    — trading a cost figure for a contract violation. The conformance test above
    missed this because it only enumerated result-carrying paths, which is the
    shape of a test that passes for the wrong reason.
    """
    from app.skill_builder.validator import validate_run_finished_result

    emitter = AGUIEmitter(thread_id="t")
    emitter.record_usage(TokenUsage(input_tokens=5))
    emitter.run_finished()
    assert _terminal(emitter) is None
    # And the shape we must never emit is genuinely rejected, so the guard above
    # is protecting against something real rather than a hypothetical.
    assert validate_run_finished_result({"usage": {"input_tokens": 5}})


def test_the_step_vocabulary_matches_the_ratified_enum_exactly():
    """Five gates, and the contract closed the set. A sixth added here without a
    contract bump would be rejected at the gateway while looking fine locally."""
    from app.skill_builder import contracts

    declared = set(contracts.run_finished_schema()["properties"]["step"]["enum"])
    assert declared == {
        "connect_or_build", "kickoff_confirmation", "connect",
        "request_declined", "review_test_results",
    }


def test_the_reason_enum_matches_our_own_closed_vocabulary():
    """The contract closed `reason` on our word (thread #15.1). If the enum here
    and the contract's ever diverge, we are emitting a value the gateway rejects
    — so they are asserted equal rather than kept in step by hand."""
    from app.skill_builder import contracts

    declared = set(contracts.run_finished_schema()["properties"]["reason"]["enum"])
    assert declared == {r.value for r in InterruptReason}


# --- #14: usage ------------------------------------------------------------


def test_sdk_usage_field_names_are_mapped_including_the_optional_cache_counts():
    """The SDK's names are NOT the wire's, and the cache counts are Optional.

    `cache_read_input_tokens` / `cache_creation_input_tokens` are None when the
    request carried no cache breakpoint. Unguarded that is a TypeError; mapped
    wrong it is a zero — and a zero is indistinguishable from a cache that never
    hit, which is the number the `cache_control` lever is tuned by.
    """
    class _Usage:
        input_tokens = 1200
        output_tokens = 340
        cache_read_input_tokens = 9000
        cache_creation_input_tokens = 512

    class _Response:
        usage = _Usage()

    assert _usage_from_response(_Response()) == TokenUsage(
        input_tokens=1200, output_tokens=340,
        cache_read_tokens=9000, cache_write_tokens=512,
    )

    class _NoCache:
        input_tokens = 5
        output_tokens = 6
        cache_read_input_tokens = None
        cache_creation_input_tokens = None

    class _R2:
        usage = _NoCache()

    assert _usage_from_response(_R2()) == TokenUsage(input_tokens=5, output_tokens=6)


def test_missing_usage_never_fails_the_turn():
    """Usage is billing metadata. Losing a conversation because a provider renamed
    a usage field would trade a cost figure for the whole turn."""
    assert _usage_from_response(object()) == TokenUsage()


def test_usage_accumulates_because_one_turn_can_make_several_calls():
    total = TokenUsage(input_tokens=10, cache_read_tokens=1) + TokenUsage(
        input_tokens=5, output_tokens=2, cache_write_tokens=3
    )
    assert total == TokenUsage(
        input_tokens=15, output_tokens=2, cache_read_tokens=1, cache_write_tokens=3
    )
    emitter = AGUIEmitter(thread_id="t")
    emitter.record_usage(TokenUsage(input_tokens=10))
    emitter.record_usage(TokenUsage(input_tokens=7, output_tokens=1))
    emitter.interrupt(InterruptReason.AWAITING_DECISION)
    assert _terminal(emitter)["usage"] == {
        "input_tokens": 17, "output_tokens": 1,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }


def test_usage_is_attached_on_every_terminal_outcome_not_just_interrupt():
    """There are four terminal call sites and the emitter owns the attachment, so
    a new one cannot forget it. A forgotten site bills that turn at zero while
    the RUN_FINISHED still carries a correct outcome."""
    for build in (
        lambda em: em.interrupt(InterruptReason.AWAITING_DECISION),
        lambda em: em.run_finished({"outcome": "tool_call", "tool": "request_finalize"}),
        lambda em: em.run_finished({"outcome": "finalized"}),
    ):
        emitter = AGUIEmitter(thread_id="t")
        emitter.record_usage(TokenUsage(input_tokens=3))
        build(emitter)
        result = _terminal(emitter)
        assert result["usage"]["input_tokens"] == 3
        # The outcome discriminator must survive the merge.
        assert "outcome" in result


def test_usage_is_omitted_entirely_when_no_model_ran():
    """All-zeros would assert that a model ran and cost nothing, which cannot
    happen. A kickoff turn is deterministic — no model call — so the key is absent
    rather than zero, and the gateway can tell "not measured" from "measured as
    cheap"."""
    res = handle_turn({
        "threadId": "s", "runId": "r",
        "messages": [{"role": "user", "content": "start"}],
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    })
    result = _terminal(res.emitter)
    assert "usage" not in result
    assert TokenUsage().is_empty() is True
    assert TokenUsage(cache_read_tokens=1).is_empty() is False


def test_usage_reaches_the_wire_through_a_real_turn():
    model = FakeChatModel(ModelDecision(
        action="propose_section", message="Proposed geography.", phase="geography",
        section={"scope": {"context_ref": "home_markets"}},
        usage=TokenUsage(input_tokens=800, output_tokens=120, cache_read_tokens=4096),
    ))
    res = handle_turn(_continuation(), model=model)
    result = _terminal(res.emitter)
    assert result["usage"] == {
        "input_tokens": 800, "output_tokens": 120,
        "cache_read_tokens": 4096, "cache_write_tokens": 0,
    }
    # The four counts stay split — a summed input figure would erase the cache
    # lever from the only data anyone could tune it with.
    assert "cache_read_tokens" in result["usage"]
