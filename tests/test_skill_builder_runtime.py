"""Turn handler — integrates protocol + context + draft + validator (PRD §3/§4/§5)."""

from app.skill_builder import draft
from app.skill_builder.model import FakeChatModel, ModelDecision
from app.skill_builder.protocol.agui import EventType
from app.skill_builder.runtime import handle_turn


def _continuation(acceptance, draft_config=None, user="ok"):
    return {
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "proposal"},
            {"role": "user", "content": user},
        ],
        "state": {"draftConfig": draft_config or {}, "acceptance": acceptance},
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }


def _kickoff_payload(**ctx):
    return {
        "threadId": "sess-1",
        "runId": "run-1",
        "messages": [{"role": "user", "content": "start"}],
        "forwardedProps": {"customer_context": ctx},
    }


def _types(res):
    return [e.type for e in res.emitter.events]


def test_kickoff_emits_expected_event_sequence():
    res = handle_turn(_kickoff_payload(organization_name="NAPA Phoenix", vertical="auto parts"))
    assert _types(res) == [
        EventType.RUN_STARTED,
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.STATE_SNAPSHOT,
        EventType.RUN_FINISHED,
    ]


def test_kickoff_first_message_reflects_customer():
    # PRD §5 acceptance: name the customer, ICP, lead type in the opener.
    res = handle_turn(
        _kickoff_payload(
            organization_name="NAPA Phoenix",
            vertical="auto parts",
            # MIXED rather than "B": a single letter would pass this assertion
            # by appearing inside any other word in the opener.
            lead_type="MIXED",
            icp_summary="regional fleets 50+ vehicles",
        )
    )
    content = res.emitter.wire_events()[2]["delta"]
    assert "NAPA Phoenix" in content
    assert "MIXED" in content
    assert "regional fleets 50+ vehicles" in content


def test_kickoff_snapshot_is_a_valid_skeleton():
    res = handle_turn(_kickoff_payload(organization_name="ACME", vertical="auto parts"))
    snapshot = res.emitter.wire_events()[4]["snapshot"]
    draft_config = snapshot["draftConfig"]
    assert draft_config["slug"] == "auto-parts-prospect-scanner"
    for phase in ("geography", "discovery", "validation", "contacts", "scoring"):
        assert phase in draft_config


def test_kickoff_interrupts_awaiting_decision():
    res = handle_turn(_kickoff_payload(organization_name="ACME"))
    finished = res.emitter.wire_events()[-1]
    assert finished["type"] == EventType.RUN_FINISHED
    assert finished["result"]["outcome"] == "interrupt"
    assert finished["result"]["reason"] == "awaiting_decision"


def test_kickoff_composes_five_layer_system_prompt():
    res = handle_turn(_kickoff_payload(organization_name="ACME"))
    assert "Platform baseline" in res.system_prompt
    assert "Customer context" in res.system_prompt
    assert "request_finalize" in res.system_prompt


def test_kickoff_with_catalog_hit_proposes_connect_not_build():
    payload = {
        "messages": [{"role": "user", "content": "start"}],
        "forwardedProps": {
            "customer_context": {
                "organization_name": "Denver Auto",
                "vertical": "auto parts",
                "lead_type": "B",
            },
            "catalog": [
                {
                    "name": "Auto Parts Scanner",
                    "slug": "auto-parts-prospect-scanner",
                    "vertical": "auto parts",
                    "lead_type": "B",
                    "skill_type": "customer",
                    "status": "active",
                }
            ],
        },
    }
    res = handle_turn(payload)
    types = _types(res)
    # Connect proposal — NO build-new skeleton snapshot on a hit.
    assert EventType.STATE_SNAPSHOT not in types
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["step"] == "connect_or_build"
    assert finished["result"]["match_slug"] == "auto-parts-prospect-scanner"
    assert "Auto Parts Scanner" in res.emitter.wire_events()[2]["delta"]


def test_kickoff_without_match_builds_new_with_snapshot():
    payload = {
        "messages": [{"role": "user", "content": "start"}],
        "forwardedProps": {
            "customer_context": {"organization_name": "ACME", "vertical": "auto parts",
                                 "lead_type": "B"},
            "catalog": [],  # empty catalog → build new
        },
    }
    res = handle_turn(payload)
    assert EventType.STATE_SNAPSHOT in _types(res)
    assert res.emitter.wire_events()[-1]["result"]["step"] == "kickoff_confirmation"


def test_continuation_reports_next_open_phase():
    payload = {
        "threadId": "sess-1",
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "kickoff"},
            {"role": "user", "content": "yes that's right"},
        ],
        "state": {"draftConfig": {}, "acceptance": {"geography": True}},
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }
    res = handle_turn(payload)
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["reason"] == "awaiting_phase_acceptance"
    assert finished["result"]["phase"] == "discovery"


def test_all_phases_accepted_offers_test_run():
    payload = {
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "accept scoring"},
        ],
        "state": {
            "draftConfig": {},
            "acceptance": {p: True for p in
                          ("geography", "discovery", "validation", "contacts", "scoring")},
        },
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }
    res = handle_turn(payload)
    assert res.emitter.wire_events()[-1]["result"]["reason"] == "awaiting_test_run"


def test_run_error_never_leaks_the_exception_text():
    """RUN_ERROR reaches the operator's browser via the gateway.

    Provider errors embed our own infrastructure identifiers — a Bedrock 403
    names `arn:aws:iam::<account>:user/...` verbatim — so the exception text must
    stay in the logs. Asserts the leak is absent, not merely that an error is
    emitted: the previous version interpolated `{exc}` and looked perfectly fine.
    """
    secret = "arn:aws:iam::082585646836:user/leo.lindo is not authorized"

    class _Exploding(FakeChatModel):
        def decide(self, **kwargs):
            raise PermissionError(secret)

    res = handle_turn(
        _continuation({"geography": True}), model=_Exploding(None)
    )
    err = res.emitter.wire_events()[-1]
    assert err["type"] == EventType.RUN_ERROR
    assert err["code"] == "internal_error"
    body = str(res.emitter.wire_events())
    assert secret not in body
    assert "PermissionError" not in body
    assert "arn:aws:iam" not in body


def test_malformed_payload_yields_run_error_not_crash():
    # messages must be a list; a string is malformed. No exception escapes.
    res = handle_turn({"messages": "not-a-list"})
    types = _types(res)
    assert EventType.RUN_ERROR in types
    err = res.emitter.wire_events()[-1]
    assert err["code"] == "invalid_input"


def _with_tool_result(result: dict):
    return {
        "messages": [
            {"role": "user", "content": "finalize"},
            {"role": "assistant", "content": "requesting finalize"},
            {"role": "tool", "tool_call_id": "tc-1", "content": result},
        ],
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }


def test_pending_rejection_routes_to_repair_not_terminal():
    payload = _with_tool_result(
        {"tool_name": "request_finalize", "status": "rejected",
         "issues": [{"location": "/scoring", "message": "literal", "kind": "org_coupling"}]}
    )
    res = handle_turn(payload)
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["reason"] == "awaiting_phase_acceptance"


def test_pending_finalize_success_is_terminal():
    payload = _with_tool_result({"tool_name": "request_finalize", "status": "succeeded"})
    res = handle_turn(payload)
    assert res.emitter.wire_events()[-1]["result"]["outcome"] == "finalized"


def test_unparseable_tool_result_becomes_run_error():
    payload = _with_tool_result({"garbage": True})  # no tool_name/status
    res = handle_turn(payload)
    assert EventType.RUN_ERROR in _types(res)


def _complete_config():
    return draft.skeleton(
        name="ACME Prospect Scanner", vertical="auto parts",
        lead_type="B",  # organizations.lead_type ENUM (A / B / MIXED)
        product_description="desc", type_="customer",
    )


def test_model_propose_section_emits_state_delta():
    model = FakeChatModel(ModelDecision(
        action="propose_section", message="Proposed discovery.",
        phase="discovery", section={"rules": [{"context_ref": "lookalike_sources"}]},
    ))
    res = handle_turn(_continuation({"geography": True}), model=model)
    types = _types(res)
    assert EventType.STATE_DELTA in types
    delta = res.emitter.wire_events()[types.index(EventType.STATE_DELTA)]["delta"]
    # Envelope-rooted, NOT "/discovery": the patch must apply to the same
    # {draftConfig, acceptance} envelope we emit in STATE_SNAPSHOT.
    assert any(op["path"] == "/draftConfig/discovery" for op in delta)
    assert not any(op["path"].startswith("/discovery") for op in delta)
    assert res.emitter.wire_events()[-1]["result"]["phase"] == "discovery"


def test_emitted_delta_applies_to_the_envelope_we_snapshot():
    """The invariant that actually matters: our own two state events must agree.

    STATE_SNAPSHOT emits the envelope `{draftConfig, acceptance}`; STATE_DELTA
    emits an RFC 6902 patch against that same envelope. They were rooted
    differently — the delta read `/geography/scope`, which cannot be applied to
    the envelope at all ("member 'geography' not found") — and nothing on the wire
    declared the difference. It stayed invisible because R2's pipe is unbuilt, so
    nobody had ever applied one. Asserting the composition, not the string.
    """
    import jsonpatch

    kickoff = handle_turn(_kickoff_payload(organization_name="ACME", vertical="auto parts"))
    envelope = kickoff.emitter.wire_events()[4]["snapshot"]
    assert set(envelope) == {"draftConfig", "acceptance"}

    model = FakeChatModel(ModelDecision(
        action="propose_section", message="Proposed geography.",
        phase="geography", section={"scope": {"context_ref": "home_markets"}},
    ))
    res = handle_turn(
        _continuation({}, draft_config=envelope["draftConfig"]), model=model
    )
    types = _types(res)
    delta = res.emitter.wire_events()[types.index(EventType.STATE_DELTA)]["delta"]

    applied = jsonpatch.apply_patch(envelope, delta, in_place=False)
    # `targeting` is the build-mode `max_discovery_rounds` seed (draft.py). It is
    # asserted here rather than excluded because the delta is exactly where it has
    # to appear — that is how the gateway persists it and the operator sees it.
    assert applied["draftConfig"]["geography"] == {
        "scope": {"context_ref": "home_markets"},
        "targeting": {"max_discovery_rounds": 4},
    }
    # Acceptance is a SIBLING and must not be disturbed by a config revision.
    assert applied["acceptance"] == envelope["acceptance"]
    # And nothing leaked to the envelope root.
    assert "geography" not in applied


def test_emitted_envelope_conforms_to_contract_4_before_and_after_a_delta():
    """Both state events must satisfy the ratified envelope contract.

    The round-trip test above proves our two events agree with EACH OTHER; this
    proves they agree with the gateway's published shape, which is the half that
    self-consistency cannot cover — our pre-`7f88631` wire was self-consistent
    in the frontend's shape sense too, and every gate was green in three repos.
    """
    from app.skill_builder.validator import validate_state_envelope

    kickoff = handle_turn(_kickoff_payload(organization_name="ACME", vertical="auto parts"))
    envelope = kickoff.emitter.wire_events()[4]["snapshot"]
    assert validate_state_envelope(envelope) == []

    model = FakeChatModel(ModelDecision(
        action="propose_section", message="Proposed geography.",
        phase="geography", section={"scope": {"context_ref": "home_markets"}},
    ))
    res = handle_turn(_continuation({}, draft_config=envelope["draftConfig"]), model=model)
    types = _types(res)
    delta = res.emitter.wire_events()[types.index(EventType.STATE_DELTA)]["delta"]

    import jsonpatch

    # The applied result is what the gateway persists, so it has to conform too.
    applied = jsonpatch.apply_patch(envelope, delta, in_place=False)
    assert validate_state_envelope(applied) == []


def test_contract_4_rejects_the_two_shapes_this_feature_actually_shipped():
    """Negative control — otherwise the conformance test above proves nothing.

    Contract #4's root is `additionalProperties: true`, so a validator that
    checked nothing but openness would pass every dict. These are the two real
    wrong shapes: frontend's pre-correction draft state, and a bare config
    (what our own emitter's pointers implied the envelope was).
    """
    from app.skill_builder.validator import validate_state_envelope

    frontend_pre_correction = {"metadata": {"name": "x"}, "phases": {"geography": {}}}
    bare_config = {"version": "1.0", "geography": {"scope": {}}}
    for bad in (frontend_pre_correction, bare_config):
        issues = validate_state_envelope(bad)
        assert issues, bad
        assert any("draftConfig" in i.message for i in issues), issues


def test_reroot_rewrites_from_as_well_as_path():
    """`move` / `copy` ops carry a `from` pointer that must also be re-rooted.

    Re-rooting only `path` would leave `from` addressing the envelope root, so the
    op would read outside `draftConfig` — a silent wrong-source copy rather than
    an error.
    """
    from app.skill_builder.protocol.agui import reroot_config_patch

    out = reroot_config_patch([{"op": "move", "from": "/discovery", "path": "/scoring"}])
    assert out == [{"op": "move", "from": "/draftConfig/discovery",
                    "path": "/draftConfig/scoring"}]


def test_model_invalid_section_does_not_emit_delta():
    # A phase not in the schema → set_section adds an unknown top-level key →
    # invalid → repair path, no STATE_DELTA.
    model = FakeChatModel(ModelDecision(
        action="propose_section", message="bad", phase="not_a_phase", section={},
    ))
    res = handle_turn(_continuation({"geography": True}), model=model)
    assert EventType.STATE_DELTA not in _types(res)
    assert res.emitter.wire_events()[-1]["type"] == EventType.RUN_FINISHED


def test_model_request_test_run_emits_tool_call_when_config_complete():
    model = FakeChatModel(ModelDecision(action="request_test_run", message="Let's test."))
    res = handle_turn(
        _continuation({"geography": True}, draft_config=_complete_config()), model=model
    )
    types = _types(res)
    assert EventType.TOOL_CALL_START in types
    assert res.emitter.wire_events()[-1]["result"]["outcome"] == "tool_call"


def test_model_connect_existing_interrupts_with_slug():
    model = FakeChatModel(ModelDecision(
        action="connect_existing", message="Connect it.", slug="auto-parts-prospect-scanner",
    ))
    res = handle_turn(_continuation({}), model=model)
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["step"] == "connect"
    assert finished["result"]["match_slug"] == "auto-parts-prospect-scanner"


def test_model_await_human_interrupts():
    model = FakeChatModel(ModelDecision(
        action="await_human", message="Which market matters most?",
        interrupt_reason="awaiting_decision",
    ))
    res = handle_turn(_continuation({}), model=model)
    assert res.emitter.wire_events()[-1]["result"]["reason"] == "awaiting_decision"


def test_continuation_without_model_uses_deterministic_fallback():
    # No model injected → still a clean turn (plumbing testable per §15).
    res = handle_turn(_continuation({"geography": True}))
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["reason"] == "awaiting_phase_acceptance"
    assert finished["result"]["phase"] == "discovery"


def test_empty_context_still_completes_a_turn():
    # Wrong-org signal path: no org data at all still yields a clean turn.
    res = handle_turn(_kickoff_payload())
    assert _types(res)[0] == EventType.RUN_STARTED
    assert _types(res)[-1] == EventType.RUN_FINISHED
    assert "unknown" in res.emitter.wire_events()[2]["delta"]


def test_phase_task_says_revise_and_warns_about_wholesale_replacement():
    """A re-opened section must be REVISED, not rebuilt.

    `set_section` replaces `config[phase]` wholesale, so a model that reads
    "propose" on a section that already has a body discards what the operator
    settled. The turn still succeeds and the config is still valid, so only
    someone who remembers the old body would ever notice.

    Raised by aeo-frontend in thread #24: once per-section change-requests are
    enabled, an accepted section's flag is cleared and it returns through
    `next_open_phase()`. An operator asking for one wording tweak would get the
    whole section rebuilt — worse than the bug that flow exists to fix.
    """
    from app.skill_builder.runtime import _phase_task
    from app.skill_builder.state import BuilderState

    populated = BuilderState.model_validate(
        {"draftConfig": {"geography": {"targeting": {"geo_strictness": "metro"}}}}
    )
    task = _phase_task(populated, "geography")
    assert "REVISE" in task
    # The *reason* has to travel with the instruction: "revise" alone does not tell
    # the model that omission deletes.
    assert "omit" in task and "deleted" in task


def test_phase_task_says_propose_for_a_section_with_no_body_yet():
    """`skeleton()` seeds every phase as {} — emptiness, not the acceptance flag,
    is what distinguishes "never authored" from "authored and re-opened"."""
    from app.skill_builder.runtime import _phase_task
    from app.skill_builder.state import BuilderState

    empty = BuilderState.model_validate({"draftConfig": {"geography": {}}})
    assert _phase_task(empty, "geography") == "Propose the 'geography' section."
    assert "REVISE" not in _phase_task(empty, "geography")


def test_continue_actually_sends_the_revise_instruction_to_the_model():
    """The call site, not just `_phase_task`.

    🔴 Added because the first version of these tests DID NOT CATCH a mutation that
    reverted `_continue` to the old ambiguous `"Propose or revise …"` string. They
    asserted `_phase_task` in isolation, so the helper was correct and unused and
    every test still passed — the precise failure this repo has a standing note
    about: mutation-test the guard, not the unit.

    Asserts on the prompt the model is actually handed.
    """
    seen: dict[str, str] = {}

    class _CapturePrompt(FakeChatModel):
        def decide(self, *, prompt, **kwargs):
            seen["rendered"] = prompt.render()
            return ModelDecision(action="await_human", message="ok")

    handle_turn(
        _continuation(
            {},  # nothing accepted -> geography is the open phase
            draft_config={"geography": {"targeting": {"geo_strictness": "metro"}}},
        ),
        model=_CapturePrompt(None),
    )
    assert "REVISE the existing 'geography' section" in seen["rendered"]
    assert "Propose or revise" not in seen["rendered"]


def test_continue_sends_propose_when_the_section_is_still_empty():
    seen: dict[str, str] = {}

    class _CapturePrompt(FakeChatModel):
        def decide(self, *, prompt, **kwargs):
            seen["rendered"] = prompt.render()
            return ModelDecision(action="await_human", message="ok")

    handle_turn(
        _continuation({}, draft_config={"geography": {}}), model=_CapturePrompt(None)
    )
    assert "Propose the 'geography' section." in seen["rendered"]
    assert "REVISE" not in seen["rendered"]


def _message_ids(res):
    """Every messageId the turn put on the wire, in order."""
    return [e["messageId"] for e in res.emitter.wire_events() if "messageId" in e]


def test_message_ids_do_not_repeat_across_turns():
    """Tracker #25: ids must be unique ACROSS turns, not just within one.

    `handle_turn` builds a fresh emitter per turn, so a per-emitter counter
    restarts at 1 every turn and turn 2 re-emits turn 1's ids. The frontend
    keys chat bubbles by `messageId`, so the second turn's text streamed into
    the first turn's bubble — with no error anywhere.

    Asserted through `handle_turn`, not by constructing an emitter directly:
    the defect was in how production builds the emitter, so an isolated
    emitter test would have passed throughout. Reverting `_default_id` to a
    bare `f"msg-{next(self._counter)}"` must fail this.
    """
    first = _message_ids(handle_turn(_kickoff_payload(organization_name="ACME")))
    second = _message_ids(handle_turn(_kickoff_payload(organization_name="ACME")))

    # Guard the guard: if the turn emitted no ids at all, the disjointness
    # assertion below would hold vacuously and prove nothing.
    assert first, "turn emitted no messageIds — the assertion below would be vacuous"

    # NOT asserted: uniqueness *within* a turn. START/CONTENT/END for one
    # assistant message deliberately share a messageId — that is the AG-UI
    # contract and `test_text_message_events_serialize_camelcase` pins it. An
    # earlier draft of this test asserted within-turn uniqueness and failed
    # against correct code; the bug is repetition ACROSS turns.
    assert not set(first) & set(second), (
        f"messageIds collided across turns: {sorted(set(first) & set(second))}"
    )


# --- vertical: the org context had none, so nothing could supply one --------


def _kickoff_text(res):
    return "".join(
        e.get("delta", "") for e in res.emitter.wire_events()
        if e["type"] == "TEXT_MESSAGE_CONTENT"
    )


def test_kickoff_asks_for_the_vertical_when_context_has_none():
    """#27 §3: the org's `industry` was null, so `vertical` finalized as null
    and R13 could never match the skill — and the opener still claimed it had
    looked, which is what made it self-perpetuating."""
    res = handle_turn(_kickoff_payload(organization_name="ACME"))
    text = _kickoff_text(res)

    assert "couldn't check whether a skill already exists" in text
    assert "tell me the vertical" in text
    assert "I didn't find an existing skill" not in text, (
        "must not claim a search it could not run"
    )


def test_kickoff_still_reports_a_real_miss_when_the_vertical_is_known():
    """The honest-miss wording must survive — with a vertical, the catalog
    check really did run and really did come back empty."""
    res = handle_turn(_kickoff_payload(organization_name="ACME", vertical="HVAC"))
    text = _kickoff_text(res)

    assert "I didn't find an existing skill" in text
    assert "tell me the vertical" not in text


def test_a_supplied_vertical_is_recorded_and_re_derives_the_slug():
    """The slug is built at kickoff, when the vertical was still unknown, so it
    had already degenerated to the bare `prospect-scanner`."""
    model = FakeChatModel(ModelDecision(
        action="await_human", message="Thanks — noted.", vertical="auto parts",
    ))
    res = handle_turn(_continuation({}), model=model)

    cfg = res.emitter  # state lives on the turn's state; assert via the delta
    deltas = [e for e in cfg.wire_events() if e["type"] == "STATE_DELTA"]
    ops = [op for d in deltas for op in d["delta"]]
    paths = {op["path"]: op["value"] for op in ops}

    assert paths.get("/draftConfig/vertical") == "auto parts"
    assert paths.get("/draftConfig/slug") == "auto-parts-prospect-scanner"


#: The lead sentence of the per-turn vertical instruction.
_VERTICAL_ASK = "The customer's VERTICAL is still unknown"


def _rendered(payload):
    """The prompt the model is actually handed for `payload`.

    Asserting on the rendered prompt rather than on a task helper: the defect
    these cover is an instruction that never REACHED the model, so a unit test
    over the string builder would have passed throughout.
    """
    seen: dict[str, str] = {}

    class _CapturePrompt(FakeChatModel):
        def decide(self, *, prompt, **kwargs):
            seen["rendered"] = prompt.render()
            return ModelDecision(action="await_human", message="ok")

    handle_turn(payload, model=_CapturePrompt(None))
    return seen["rendered"]


def _continuation_with_ctx(ctx, draft_config=None):
    payload = _continuation({}, draft_config=draft_config)
    payload["forwardedProps"]["customer_context"] = ctx
    return payload


def test_an_authoring_turn_asks_for_the_vertical_when_the_config_has_none():
    """The production defect: the kickoff asks the OPERATOR, and by the turn they
    answer the task is `Propose the 'geography' section` — nothing anywhere told
    the MODEL to return that answer in `vertical`. So it landed only when the
    model volunteered an optional field, and 6 of 12 real sessions finalized
    `vertical: null` with the degenerate `prospect-scanner` slug after the agent
    had confirmed a vertical in prose (aeo-frontend, 2026-08-17).

    The old `test_a_supplied_vertical_is_recorded_and_re_derives_the_slug` could
    not catch it: it injects `vertical="auto parts"` into the decision, i.e. it
    only ever covered the branch that already worked.
    """
    rendered = _rendered(_continuation({}, draft_config={"geography": {}}))

    assert _VERTICAL_ASK in rendered
    assert "`vertical` field of this" in rendered
    # The distinction the model kept getting wrong: prose is not persistence.
    assert "does NOT record it" in rendered


def test_a_placeholder_industry_is_named_as_the_reason_and_contradicted():
    """`vertical` maps "Other" to None, but `as_prompt_block` serializes the raw
    blob, so the model still sees `industry: "Other"`. The instruction has to
    contradict the data explicitly — otherwise the model believes the blob over
    us, which is precisely the state that shipped.
    """
    rendered = _rendered(_continuation_with_ctx(
        {"organization_name": "Lee Company", "organization": {"industry": "Other"}}
    ))

    assert _VERTICAL_ASK in rendered
    assert "catch-all placeholder" in rendered
    assert "must NOT be read as the answer" in rendered


def test_a_wholly_absent_industry_gets_the_other_reason():
    """Two different causes, two different sentences. If both said "no industry",
    the placeholder case would keep contradicting a value the model can see."""
    rendered = _rendered(_continuation_with_ctx({"organization_name": "ACME"}))

    assert _VERTICAL_ASK in rendered
    assert "carries no industry at all" in rendered
    assert "catch-all placeholder" not in rendered


def test_the_vertical_request_disappears_once_the_config_carries_one():
    """Derived from the config rather than remembered, so it stops by itself.

    Load-bearing for edit sessions: their config always carries a vertical (both
    finalize gates refuse a null), so without this they would be nagged on every
    turn to chase a value that is already set.
    """
    rendered = _rendered(_continuation({}, draft_config={"vertical": "HVAC"}))

    assert _VERTICAL_ASK not in rendered
    assert "catch-all placeholder" not in rendered


def test_a_blank_vertical_still_counts_as_missing_for_both_readers():
    """`_apply_vertical` treats a whitespace-only vertical as a gap it may fill.
    A plain truthiness check would read "   " as PRESENT and go silent, so the
    instruction and the overwrite guard would disagree about what missing means --
    the same two-readers-disagree defect this change fixes, reintroduced one layer
    down. They now share `_has_vertical`.
    """
    rendered = _rendered(_continuation({}, draft_config={"vertical": "   "}))
    assert _VERTICAL_ASK in rendered, "a blank vertical must still be asked for"

    # ...and the other reader agrees: it fills the blank rather than preserving it.
    model = FakeChatModel(ModelDecision(
        action="await_human", message="ok", vertical="insurance",
    ))
    res = handle_turn(
        _continuation({}, draft_config={"vertical": "   "}), model=model
    )
    ops = [
        op for e in res.emitter.wire_events() if e["type"] == "STATE_DELTA"
        for op in e["delta"]
    ]
    paths = {op["path"]: op["value"] for op in ops}
    assert paths.get("/draftConfig/vertical") == "insurance"


def test_a_context_supplied_vertical_is_never_overwritten():
    """The org's runtime context is authoritative; the ask exists only to fill
    a genuine gap."""
    model = FakeChatModel(ModelDecision(
        action="await_human", message="ok", vertical="something else",
    ))
    res = handle_turn(
        _continuation({}, draft_config={"vertical": "HVAC"}), model=model
    )
    ops = [
        op for e in res.emitter.wire_events() if e["type"] == "STATE_DELTA"
        for op in e["delta"]
    ]
    assert not any(op["path"] == "/draftConfig/vertical" for op in ops)


# --- all sections accepted: the state that could never act (#27) ------------


def _all_accepted(draft_config=None, user="ok"):
    payload = _continuation(
        {p: True for p in
         ("geography", "discovery", "validation", "contacts", "scoring")},
        draft_config=draft_config,
        user=user,
    )
    return payload


def _complete_draft():
    return draft.skeleton(
        name="ACME Prospect Scanner",
        vertical="auto parts",
        lead_type="B",
        product_description="Prospect scanner for the auto parts vertical.",
        type_="customer",
    )


def test_all_accepted_state_reaches_the_model():
    """It used to short-circuit before `model.decide`, so every later turn was
    byte-identical and the operator's "yes, run a test" was never seen by
    anything that could act on it. Backend proved it from `usage` being absent
    on both turns — by contract that means no model ran."""
    seen = {}

    class _Recording(FakeChatModel):
        def decide(self, **kwargs):
            seen["called"] = True
            seen["open_phase"] = kwargs["open_phase"]
            return ModelDecision(action="await_human", message="Ready when you are.")

    handle_turn(_all_accepted(), model=_Recording(None))

    assert seen.get("called"), "the model must be consulted once authoring is done"
    assert seen["open_phase"] is None, "there is no next section to propose"


def test_all_accepted_can_now_emit_a_tool_call():
    """`TOOL_CALL_*` has never fired in any repo. This is the path that makes it
    reachable: the operator asks, the model chooses `request_test_run`, and the
    gateway executes it.

    Uses a complete draft, because the tool gate legitimately blocks an
    incomplete one — a test asserting emission on a config that could never
    pass the gate would prove nothing.
    """
    model = FakeChatModel(ModelDecision(
        action="request_test_run", message="Running a test now.",
    ))
    res = handle_turn(
        _all_accepted(draft_config=_complete_draft(), user="yes, run a test"),
        model=model,
    )

    assert EventType.TOOL_CALL_START in _types(res)
    assert res.emitter.wire_events()[-2]["type"] == EventType.TOOL_CALL_END


def test_all_accepted_without_a_model_still_serves_a_coherent_turn():
    """The no-model path is what every sibling repo built against before model
    access landed, and it must never emit a tool call — a gateway side effect
    must not originate from a stub."""
    res = handle_turn(_all_accepted())
    assert EventType.TOOL_CALL_START not in _types(res)
    assert res.emitter.wire_events()[-1]["result"]["reason"] == "awaiting_test_run"
