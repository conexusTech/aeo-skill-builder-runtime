"""Edit mode — revising an existing skill (skill-update thread U1 / D).

The invariant these guard is *destructive and silent*: the build kickoff
overwrites `state.draft_config` with a fresh skeleton and snapshots it, so a
seeded edit session that takes the build path loses the skill's config before
the operator types anything, and the gateway persists the wipe. Nothing errors.

Each test below is written so that deleting the thing it names actually fails
it — the recurring failure on this feature is a test that covers a helper while
nothing asserts the CALL SITE uses it.
"""

from app.skill_builder import draft
from app.skill_builder.model import FakeChatModel, ModelDecision
from app.skill_builder.protocol.agui import EventType
from app.skill_builder.runtime import handle_turn

#: A config shaped like one seeded from a finalized skill: every section
#: authored, so `_kickoff_edit` treats it as a real edit rather than refusing.
SEEDED = {
    "version": "1.0",
    "run_parameters": {},
    "name": "Commercial Roofing Prospect Scanner",
    "slug": "commercial-roofing-prospect-scanner",
    "vertical": "commercial roofing",
    "geography": {"home_markets": {"context_ref": "home_markets"}},
    "discovery": {"sources": {}},
    "validation": {"disqualifiers": {"context_ref": "disqualifiers"}},
    "contacts": {"titles": {"context_ref": "decision_maker_titles"}},
    "scoring": {"factors": []},
}

#: A catalog entry that WOULD match the customer context below. Present so the
#: "no library pitch in edit mode" tests are not vacuous — see
#: `test_the_same_payload_in_build_mode_does_pitch_the_catalog`.
CATALOG = [
    {
        "name": "Commercial Roofing Scanner",
        "slug": "commercial-roofing-prospect-scanner",
        "vertical": "commercial roofing",
        "lead_type": "B",
        "skill_type": "customer",
        "status": "active",
    }
]

CUSTOMER = {
    "organization_name": "Franklin Roofing",
    "vertical": "commercial roofing",
    "lead_type": "B",
    "icp_summary": "multi-site facilities portfolios",
}


def _payload(*, mode="edit", config=None, catalog=None):
    """A first turn: no assistant message, so `is_kickoff` is True."""
    props = {"customer_context": CUSTOMER, "catalog": catalog if catalog is not None else CATALOG}
    if mode is not None:
        props["mode"] = mode
    return {
        "threadId": "sess-edit-1",
        "runId": "run-1",
        "messages": [{"role": "user", "content": "start"}],
        "state": {
            "draftConfig": SEEDED if config is None else config,
            "acceptance": dict.fromkeys(
                ("geography", "discovery", "validation", "contacts", "scoring"), True
            ),
        },
        "forwardedProps": props,
    }


def _types(res):
    return [e.type for e in res.emitter.events]


def test_edit_kickoff_emits_nothing_that_would_replace_the_seeded_config():
    """THE defect U1 exists to prevent.

    ⚠️ Asserted on the EMITTED EVENTS, not on the input dict. `_kickoff` does
    `run_input.state.draft_config = skeleton`, which rebinds a pydantic field —
    it never mutates the caller's dict, so `payload[...] == SEEDED` passes
    whether or not the clobber happened. That was the first version of this
    test and it could not fail. What actually reaches the gateway, and what it
    persists, is the event stream.
    """
    res = handle_turn(_payload())
    types = _types(res)
    assert EventType.STATE_SNAPSHOT not in types
    assert EventType.STATE_DELTA not in types


def test_the_same_seeded_payload_in_build_mode_really_does_clobber_it():
    """Critical negative control for the test above — and the proof that the
    defect is real rather than a story about the code.

    Same seeded config, only `mode` differs. Build mode snapshots a skeleton
    whose sections are empty, which is what the gateway would persist over the
    operator's skill.
    """
    res = handle_turn(_payload(mode="build", catalog=[]))
    snapshot = res.emitter.wire_events()[4]["snapshot"]["draftConfig"]

    assert snapshot != SEEDED
    assert snapshot["scoring"] != SEEDED["scoring"]
    assert snapshot["geography"] != SEEDED["geography"]


def test_edit_kickoff_does_not_pitch_the_catalog():
    res = handle_turn(_payload())
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["step"] != "connect_or_build"
    assert "match_slug" not in finished["result"]


def test_the_same_payload_in_build_mode_does_pitch_the_catalog():
    """Critical negative control.

    Without this, the test above passes even if the catalog simply never
    matched — proving nothing about edit mode. Same customer, same catalog,
    only `mode` differs.
    """
    res = handle_turn(_payload(mode="build"))
    finished = res.emitter.wire_events()[-1]
    assert finished["result"]["step"] == "connect_or_build"


def test_edit_kickoff_reflects_the_customer_and_names_the_skill():
    # PRD §5's wrong-org check applies to an edit exactly as to a build, and the
    # operator also has to see WHICH skill they opened.
    res = handle_turn(_payload())
    content = res.emitter.wire_events()[2]["delta"]
    assert "Franklin Roofing" in content
    assert "multi-site facilities portfolios" in content
    assert "Commercial Roofing Prospect Scanner" in content
    assert "commercial-roofing-prospect-scanner" in content


def test_edit_kickoff_interrupts_on_a_known_step():
    """`step` picks the operator's control, so an invented value renders wrong.

    Pinned against the documented vocabulary rather than against our own
    constant: this is a wire value other repos read.
    """
    res = handle_turn(_payload())
    result = res.emitter.wire_events()[-1]["result"]
    assert result["outcome"] == "interrupt"
    assert result["reason"] == "awaiting_decision"
    assert result["step"] in {
        "connect_or_build",
        "kickoff_confirmation",
        "connect",
        "request_declined",
        "review_test_results",
    }


def test_edit_kickoff_tells_the_model_it_is_revising():
    """The prompt half of D — without it the model re-runs the interview."""
    res = handle_turn(_payload())
    assert "EDIT session" in res.system_prompt
    assert "Do NOT re-interview" in res.system_prompt


def test_edit_mode_with_no_authored_section_refuses_instead_of_building():
    """Falling back to the build kickoff here is the DANGEROUS option.

    There is no config to destroy, so it looks harmless — but the operator would
    then author a skill from scratch inside a session the gateway believes is an
    edit, and finalize would overwrite the real skill's config with it.
    """
    res = handle_turn(_payload(config={"version": "1.0"}))
    types = _types(res)
    assert EventType.RUN_ERROR in types
    # The tell that it did not silently become a build.
    assert EventType.STATE_SNAPSHOT not in types


def test_an_unrecognised_mode_falls_back_to_build():
    """Fails CLOSED. Reading an unknown mode as `edit` would start a real build
    with no skeleton, i.e. no config at all."""
    res = handle_turn(_payload(mode="EDIT_v2", catalog=[]))
    assert EventType.STATE_SNAPSHOT in _types(res)


def test_mode_is_case_insensitive_and_tolerates_whitespace():
    res = handle_turn(_payload(mode="  Edit  "))
    assert EventType.STATE_SNAPSHOT not in _types(res)


def test_an_absent_mode_is_a_build_so_existing_traffic_is_unchanged():
    """Neither repo has to deploy first."""
    res = handle_turn(_payload(mode=None, catalog=[]))
    assert EventType.STATE_SNAPSHOT in _types(res)


# -- the slug guard (U1 / 🅕 "guard it") ------------------------------------


def _continuation(*, mode, config, vertical="commercial roofing"):
    """A turn where the model supplies a vertical — the one path that patches
    `/slug`. Not a kickoff: there is an assistant message."""
    props = {"customer_context": {"organization_name": "Franklin Roofing"}}
    if mode is not None:
        props["mode"] = mode
    return {
        "threadId": "sess-edit-2",
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "proposal"},
            {"role": "user", "content": "it's commercial roofing"},
        ],
        "state": {"draftConfig": config, "acceptance": {}},
        "forwardedProps": props,
    }


def _delta_ops(res, suffix):
    """Ops whose path ends in `suffix`.

    Paths on the wire are rebased onto the state envelope (`/draftConfig/slug`,
    not `/slug`), so matching the bare config path finds nothing and every
    assertion built on it passes vacuously.
    """
    return [
        op
        for event in res.emitter.wire_events()
        if event["type"] == EventType.STATE_DELTA
        for op in event["delta"]
        if str(op.get("path", "")).endswith(suffix)
    ]


def test_editing_never_renames_the_skill():
    """`skills.slug` is load-bearing downstream (`runtime_slug`, catalog
    `taskRef`), so re-deriving it renames a live, connected skill."""
    config = {**SEEDED, "slug": "the-operators-own-slug"}
    del config["vertical"]  # the only state in which _apply_vertical proceeds
    model = FakeChatModel(
        ModelDecision(action="await_human", message="noted", vertical="commercial roofing")
    )
    res = handle_turn(_continuation(mode="edit", config=config), model=model)

    assert _delta_ops(res, "/slug") == []
    # The vertical itself IS still recorded — the guard is narrow, not a mute.
    assert [op["value"] for op in _delta_ops(res, "/vertical")] == ["commercial roofing"]


def test_building_still_re_derives_the_slug():
    """Critical negative control for the test above.

    Without it, an `_apply_vertical` that never patched `/slug` at all would
    pass — and #27 §3's fix (re-derive once the vertical is known) would be
    silently reverted.
    """
    config = {"version": "1.0", "run_parameters": {}, "slug": "prospect-scanner"}
    model = FakeChatModel(
        ModelDecision(action="await_human", message="noted", vertical="commercial roofing")
    )
    res = handle_turn(_continuation(mode="build", config=config), model=model)

    assert [op["value"] for op in _delta_ops(res, "/slug")] == [
        "commercial-roofing-prospect-scanner"
    ]


# -- the round-cap seed, at the CALL SITE ------------------------------------


def _valid_config():
    """A config that passes the whole-config gate `_apply_decision` runs.

    `SEEDED` deliberately does not: `discovery.sources` is empty and its
    `contacts.titles` ref is an unpublished key, and `propose_section` lints the
    WHOLE config, so a turn built on it never reaches a delta. Using it here would
    have made both tests below pass vacuously on an interrupt.
    """
    cfg = draft.skeleton(
        name="Franklin Roofing Prospect Scanner",
        vertical="commercial roofing",
        lead_type="B",
        product_description="commercial re-roofing and maintenance",
    )
    cfg, _ = draft.set_section(
        cfg, "geography", {"home_markets": {"context_ref": "home_markets"}},
        edit_mode=True,
    )
    return cfg


def _propose_geography(*, mode):
    """A revision turn that re-proposes geography, in build or edit mode.

    The body authors `targeting.geo_strictness` — a sibling knob — so the turn
    emits a `/geography/targeting` op either way and the difference between the
    modes is the CONTENT of that op, not whether one exists.
    """
    model = FakeChatModel(ModelDecision(
        action="propose_section",
        message="Proposed geography.",
        phase="geography",
        section={
            "home_markets": {"context_ref": "home_markets"},
            "targeting": {"geo_strictness": "state"},
        },
    ))
    return handle_turn(_continuation(mode=mode, config=_valid_config()), model=model)


def test_build_mode_seeds_the_round_cap_through_handle_turn():
    """`draft.set_section` taking an `edit_mode` flag proves nothing on its own —
    the recurring failure on this feature is a covered helper whose CALL SITE
    never passes the argument. This asserts the wire."""
    ops = _delta_ops(_propose_geography(mode=None), "/geography/targeting")
    assert ops, "the round cap never reached the wire"
    assert ops[-1]["value"] == {"geo_strictness": "state", "max_discovery_rounds": 4}


def test_edit_mode_does_not_seed_the_round_cap_through_handle_turn():
    ops = _delta_ops(_propose_geography(mode="edit"), "/geography/targeting")
    # Not vacuous: the turn DID revise targeting...
    assert ops, "no targeting delta at all"
    # ...it just did not invent a knob the finalized skill omits.
    assert ops[-1]["value"] == {"geo_strictness": "state"}
