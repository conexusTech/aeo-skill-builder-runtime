"""Seven-layer prompt composition (PRD §9)."""

from app.skill_builder import contracts
from app.skill_builder.context import CONTEXT_FENCE_OPEN, CustomerContext
from app.skill_builder.prompt import compose


def _compose():
    ctx = CustomerContext({"organization_name": "ACME", "vertical": "auto parts"})
    return compose(customer_context=ctx, task="Propose the geography section.",
                   tools=contracts.tool_schemas(),
                   context_field_keys=contracts.context_field_keys(),
                   config_positions=contracts.config_positions(),
                   runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema())


def test_layers_are_in_stable_to_volatile_order():
    comp = _compose()
    assert comp.layer_names() == [
        "Platform baseline",
        "Customer context",
        "Agent identity",
        "Context-field bindings",
        "Section shapes",
        "Task",
        "Tools",
    ]


def test_baseline_contains_injection_guardrail():
    rendered = _compose().render()
    # The guardrail must name the fence and forbid obeying data-as-instructions.
    assert CONTEXT_FENCE_OPEN in rendered
    assert "untrusted" in rendered.lower()
    assert "never" in rendered.lower() or "not act on it" in rendered.lower()


def test_customer_context_is_fenced_in_the_prompt():
    rendered = _compose().render()
    assert "ACME" in rendered
    assert rendered.count(CONTEXT_FENCE_OPEN) >= 1


def test_task_and_tools_layers_present():
    rendered = _compose().render()
    assert "Propose the geography section." in rendered
    assert "request_test_run" in rendered
    assert "request_finalize" in rendered


def test_identity_states_vertical_not_org_invariant():
    rendered = _compose().render()
    assert "VERTICAL" in rendered
    assert "context-field key" in rendered


def test_split_separates_stable_prefix_from_volatile_suffix():
    stable, volatile = _compose().split()
    # Cacheable stable prefix: baseline + context + identity + bindings + shapes.
    # Both contract-derived layers must land here, or every turn re-sends the key
    # vocabulary and every section shape -- the opposite of the cost lever.
    assert "Platform baseline" in stable
    assert "Customer context" in stable
    assert "Agent identity" in stable
    assert "Context-field bindings" in stable
    assert "Section shapes" in stable
    assert "Section shapes" not in volatile
    # Volatile suffix: the per-turn task + tool schemas.
    assert "Propose the geography section." in volatile
    assert "request_test_run" in volatile
    # No overlap.
    assert "Platform baseline" not in volatile
    assert "request_test_run" not in stable


def test_bindings_layer_gives_the_syntax_and_the_closed_key_list():
    """R12: stating the rule without the vocabulary is not actionable.

    The identity layer says "reference a well-known context-field key"; without
    the keys the model must invent them, and the gateway rejects an unknown key
    HARD. That failure is invisible to config validation — section internals are
    `additionalProperties: true`, so a wrong binding passes the schema and fails
    only at test-run/finalize.
    """
    rendered = _compose().render()
    # Exact binding shape, not just the concept.
    assert '{"context_ref": "home_markets"}' in rendered
    # `default` is NAMED so the model knows the shape it must not author — but
    # only as a prohibition. This assertion used to require a worked EXAMPLE of a
    # literal default (`{"context_ref": "excluded_markets", "default": ["Reno"]}`),
    # and #28 is what that cost: on a Nashville org the model reproduced the
    # shape with Austin values, systematically. The rule and the counter-example
    # coexisted and the example won, so the example is gone and this now pins the
    # absence of one (see test_prompt_shows_no_worked_example_of_a_literal_default).
    assert "`default`" in rendered
    assert '"default":' not in rendered, "must not demonstrate what it forbids"
    # The closed vocabulary must actually be enumerated.
    for key in contracts.context_field_keys():
        assert key in rendered, f"{key} missing from the prompt"
    # Unprefixed is the canonical form and the prompt must say so.
    assert "customer.home_markets" in rendered  # named as the WRONG form
    assert "closed" in rendered.lower()


def test_bindings_layer_key_order_is_deterministic():
    """The layer sits in the CACHED prefix, so it must be byte-stable.

    `frozenset` iteration order varies between processes under string hash
    randomisation. Emitting keys in set order would make the "stable" prefix
    differ per invocation and the prompt cache would silently never hit — a cost
    lever that appears to be working.
    """
    keys = frozenset({"home_markets", "disqualifiers", "competitors", "industry"})
    ctx = CustomerContext({"organization_name": "ACME"})
    renders = {
        compose(customer_context=ctx, task="t", tools={},
                context_field_keys=keys,
                config_positions=contracts.config_positions(),
                runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema()).render()
        for _ in range(5)
    }
    assert len(renders) == 1
    # Assert on the enumerated bullet lines, not raw substring positions: the
    # layer's worked examples also mention key names, so `index()` would find the
    # example rather than the list entry.
    rendered = renders.pop()
    # The layer now carries THREE bullet lists: the closed key vocabulary, the
    # enforced config positions, and the runtime-populated positions nobody may
    # author. Partition on "is this bullet a bare key" rather than on any one
    # punctuation mark -- the arrow-based split broke the moment a third list
    # arrived using a different separator, and widening the assertion instead
    # would have stopped checking key ORDER, which is the whole point here.
    bullets = [
        line.strip()[2:]
        for line in rendered.splitlines()
        if line.startswith("  - ")
    ]
    key_bullets = [b for b in bullets if " " not in b]  # keys are bare identifiers
    annotated = [b for b in bullets if " " in b]
    assert key_bullets == sorted(keys)
    # Positions and forbidden entries come from JSON arrays, so published order IS
    # the stable order -- and the set-of-renders assertion above already proves the
    # byte-stability guarantee covers them too.
    assert any("→" in b for b in annotated), "config positions must be enumerated"
    assert any("runtime fills it" in b for b in annotated), (
        "runtime-populated positions must be enumerated"
    )
    assert len(renders) == 0


# --- the shapes layer is DERIVED from the schema (2026-08-05) ----------------


def test_section_shapes_are_derived_not_transcribed():
    """The whole point of deriving: a shape the schema declares cannot disagree
    with what we teach, and a shape it stops declaring stops being taught.

    Replaced a hand-maintained constant whose transcription was wrong in two
    places — the position that produced six defects across this feature.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    schema = contracts.config_schema()
    rendered = _section_shapes_layer(schema)
    # Every declared internal of every shaped section must appear.
    for section in ("geography", "discovery", "validation", "contacts"):
        for key in schema["properties"][section]["properties"]:
            assert key in rendered, f"{section}.{key} missing from the prompt"


def test_pipeline_is_declared_by_the_schema_and_deliberately_not_taught():
    """🔴 PERMANENT by product decision. Do not "fix" this by adding `pipeline`.

    ⚠️ This is not a gap awaiting closure. **The PO ruled (2026-08-21, reaffirmed twice,
    gateway `4a21468`) that the stage vocabulary is STATIC IN CODE** -- one shared
    `TIMELINE_STAGES` ladder across both skill types, no config authoring, no builder
    change. The gateway asked us explicitly: *"Do not add a sixth authoring section. Not
    now and not later."* So there is nothing for the builder to author, and this test is
    the standing statement of that contract rather than a temporary pin.

    ### Why it reads as an omission if you do not know the history

    The gateway declared six `pipeline` keys (`3169d10`) and asked us to re-pin and teach
    them, on the stated premise that our builder "keeps emitting {key, label}". **It
    emitted neither.** `pipeline` appears nowhere in this runtime's Python -- not in
    `draft.skeleton()`, not in `_SHAPED_SECTIONS`, nowhere -- so every skill had always
    run on their code default and no builder-authored ladder had ever existed. That
    correction is what turned "static in code" from a compromise into the obvious answer.

    ### And there was never a slot to author one into

    Still true, and still the reason teaching the shape would be actively harmful:

    * `propose_section` carries a section BODY, never a section NAME -- it applies to
      whichever of the five authoring phases is open (`draft._unwrap_self_named(phase,
      ...)`), so the model cannot direct a proposal at `pipeline`.
    * `agui_state_envelope.json` -- the gateway's own pinned contract -- keys
      `acceptance` by "the five authoring section names (geography, discovery,
      validation, contacts, scoring)".

    Teaching the shape without a destination is the #30 failure reproduced exactly: the
    model, told `pipeline.stages` is authorable, writes the stages into whichever phase
    happens to be open. That lands as `draftConfig.scoring.stages`, which VALIDATES
    (sections are `additionalProperties: true`), passes the R12 lint, applies cleanly as
    a delta, and is then read by nobody. Silent, and indistinguishable from a vertical
    that has no ladder -- the same shape as the defect the gateway was fixing when it
    asked.

    ### The pin still matters, which is why both halves are asserted

    🔑 A skill that DOES declare `config.pipeline.stages[]` still overrides the shared
    ladder -- the gateway resolves `declared ?? TIMELINE_STAGES` with no branch on skill
    type. So the six keys are live, not dead, and a stale pin would misdescribe a path
    that is still read. Either assertion alone rots:

      1. the schema DOES declare the keys -- the re-pin is current; and
      2. we do NOT teach them -- the decision stays visible instead of looking like
         something nobody got around to.
    """
    from app.skill_builder.prompt import _SHAPED_SECTIONS, _section_shapes_layer

    schema = contracts.config_schema()

    # 1. The re-pin carries the gateway's declaration.
    stage_props = schema["properties"]["pipeline"]["properties"]["stages"]["items"][
        "properties"
    ]
    assert {
        "key",
        "label",
        "minMonths",
        "maxMonths",
        "requiresContact",
        "color",
        "description",
    } <= set(stage_props), f"the pin is stale; saw {sorted(stage_props)}"
    assert "signalFields" in schema["properties"]["pipeline"]["properties"]

    # 2. And we do not teach it.
    assert "pipeline" not in _SHAPED_SECTIONS
    rendered = _section_shapes_layer(schema)
    for key in ("minMonths", "maxMonths", "signalFields"):
        assert key not in rendered, (
            f"{key} reached the prompt. The stage vocabulary is static in code by PO "
            "decision (gateway 4a21468) and there is no slot to propose a `pipeline` "
            "into, so the model has just been taught to author into a section that "
            "will be silently dropped. Revert that change rather than this test."
        )


def test_a_schema_only_shape_appears_without_touching_this_module():
    """Drift-proofing, asserted rather than claimed: inject a new section key into
    a copy of the schema and it must be taught with no code change here."""
    import copy

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["contacts"]["properties"]["invented_later"] = {
        "type": "string", "description": "A key nobody has written code for.",
    }
    rendered = _section_shapes_layer(schema)
    assert "invented_later" in rendered
    assert "A key nobody has written code for." in rendered


def test_nested_definitions_expand_and_carry_their_reasoning():
    """`targeting` renders as a bare 'object' without the nested pass, losing that
    `use_zip_discovery` falsy means SKIP."""
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(contracts.config_schema())
    assert "use_zip_discovery" in rendered
    assert "FALSY MEANS SKIP" in rendered


def _three_level_schema():
    """A section property nested three deep, shaped like the prospect scoring
    redesign's `gate` block — the shape that forced `_MAX_NESTING_DEPTH` to 2."""
    import copy

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["scoring"]["properties"]["gate"] = {
        "type": "object",
        "description": "LEVEL ONE description.",
        "properties": {
            "target_market": {
                "type": "object",
                "description": "LEVEL TWO description.",
                "properties": {
                    "allowed_states_from": {
                        "type": "string",
                        "description": "LEVEL THREE description.",
                        "properties": {
                            "deeper": {
                                "type": "string",
                                "description": "LEVEL FOUR description.",
                            }
                        },
                    }
                },
            }
        },
    }
    return schema


def test_a_leaf_two_levels_below_a_section_property_reaches_the_model():
    """The prospect scoring redesign put every field that carries meaning two
    levels below `scoring`. Measured against the real renderer before the bound
    moved: 12 of 12 leaf descriptions were dropped, the model was handed
    `target_market — object` and nothing else, and the state-normalisation rule
    that is the actual fix for the dead `region_bonus` axis was among them.

    Asserts the KEY and its DESCRIPTION separately: a key name with no reasoning
    beside it is the `{"max": N}` defect the gate block exists to stop, so
    rendering the name alone would satisfy a laxer test and teach nothing.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(_three_level_schema())
    assert "allowed_states_from" in rendered
    assert "LEVEL THREE description." in rendered


def test_the_expansion_stops_at_two_levels():
    """The bound is the whole reason this is a renderer and not a schema walker,
    whose bugs would surface as authoring errors.

    The test this replaces was named `..._one_level_and_no_further` and asserted
    only that level-two content was PRESENT — it never checked "no further", so
    the name over-claimed against what ran. Both halves are asserted here.
    """
    from app.skill_builder.prompt import _MAX_NESTING_DEPTH, _section_shapes_layer

    assert _MAX_NESTING_DEPTH == 2
    rendered = _section_shapes_layer(_three_level_schema())
    assert "LEVEL TWO description." in rendered
    assert "LEVEL FOUR description." not in rendered
    assert "deeper" not in rendered


def test_an_arrays_item_properties_are_named_but_not_described():
    """A deliberate boundary, and the measurement is the reason for it.

    `_type_hint` names an object item's KEYS (`list of {...}`) and stops, so a
    description written on an array item's property reaches the model at no depth.
    Extending into them is NOT free the way the depth change was: `scoring.factors`
    alone carries 14 described item properties (~4,300 chars) and
    `validation.lanes` six more, so it would drag thousands of characters into
    every prompt — the `_NOTE_BUDGET` spillover this change was argued as not
    having. A schema author puts array-item semantics on the array property
    itself, where it renders.
    """
    import copy

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["scoring"]["properties"]["bonus"] = {
        "type": "object",
        "properties": {
            "bands": {
                "type": "array",
                "description": "ARRAY PROPERTY description.",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "table": {
                            "type": "object",
                            "description": "ITEM PROPERTY description.",
                        },
                    },
                },
            }
        },
    }
    rendered = _section_shapes_layer(schema)
    assert "list of {name, table?}" in rendered      # keys are named
    assert "ARRAY PROPERTY description." in rendered  # the array's own note renders
    assert "ITEM PROPERTY description." not in rendered


def test_a_direct_context_ref_property_is_not_re_expanded():
    """Exercises the `context_ref in props` guard, which the real schema does NOT
    reach — `boundStringList` is an `anyOf`, so it has no `properties` and returns
    early for an unrelated reason.

    Found by mutation: removing the guard changed nothing, and the assertion that
    looked like it covered it was passing on the `anyOf` path. So the guard is
    tested against a schema that actually triggers it, or it is not tested at all.
    A binding is explained once by the bindings layer; re-explaining `context_ref`
    per property would bury the shapes explained nowhere else.
    """
    import copy

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["contacts"]["properties"]["bound_directly"] = {
        "$ref": "#/$defs/contextRef"
    }
    rendered = _section_shapes_layer(schema)
    assert "bound_directly" in rendered
    assert "Well-known org context-field key" not in rendered


def test_scoring_teaches_the_engines_knobs_and_never_invites_invention():
    """`scoring` was never ratified into the schema (#17/#18 ratified four
    section shapes; this was not one), so the layer used to render "internals not
    yet specified — author what the vertical needs". A model read that as licence:
    thread #30's first real customer scan ranked HVAC prospects with a scoring
    shape the engine never looked at, and two sessions on one runtime invented
    two incompatible shapes that both passed every gate.

    The permissive wording is the defect, so it is asserted absent — not merely
    the knobs asserted present. A future edit could reintroduce the invitation
    alongside the knob list and the positive assertion alone would stay green.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(contracts.config_schema())
    assert "scoring" in rendered

    # SEVEN, not five. The cross-repo answer that supplied this list omitted the
    # last two (`av_lead_scanner.py:938-939`), and a consumed-key lint built from
    # the short list would tell the model to delete two keys that do take effect.
    for knob in (
        # `factors` came LAST chronologically and is FIRST in importance: the
        # engine began reading it on 2026-08-12, hours after the other seven were
        # pinned, and it is now the 40-point ICP-fit axis. A skill authored
        # without it loses that axis entirely.
        "factors",
        "completeness", "fit", "region_bonus", "multi_source",
        "pipeline", "ai_adjustment", "score_cap",
    ):
        assert knob in rendered, f"engine knob {knob!r} is not taught"

    assert "not yet specified" not in rendered
    assert "author what the" not in rendered
    # 🔴 The prompt must NOT claim an override replaces a knob's sub-keys.
    # v13-v15 said: "An override REPLACES a knob's sub-keys wholesale rather than
    # merging, so restate every sub-key you want kept." That is FALSE.
    # `_deep_get` is `dict(default)` + `.update(override)` — a SHALLOW MERGE, so
    # sub-keys the config does not mention are KEPT from the engine default.
    #
    # It was not a harmless inaccuracy: a model following "restate every sub-key
    # you want kept" on `fit` would have copied the engine's church-AV
    # `keyword_scores` verbatim into an HVAC config — manufacturing the exact
    # cross-vertical contamination #30 exists to remove. It never fired because
    # no skill has been authored since v12. Removed with the pinned fallback on
    # 2026-08-13; asserted absent so it cannot return.
    assert "REPLACES" not in rendered
    assert "restate every sub-key" not in rendered


# --- renderer robustness, from the 2026-08-05 audit pass ---------------------


def test_odd_schema_nodes_are_skipped_not_raised():
    """This renderer runs inside compose() on EVERY turn, and the schema is
    swappable — so an AttributeError here is a dead conversation surfaced as an
    opaque RUN_ERROR, not a worse prompt. `true` is a legal subschema, `$defs`
    could be malformed, a description need not be a string. Each of these raised
    before the audit."""
    from app.skill_builder.prompt import _section_shapes_layer

    real = contracts.config_schema()
    for label, mutate in {
        "section is not an object": lambda s: s["properties"].__setitem__("geography", True),
        "property is `true`": lambda s: s["properties"]["contacts"]["properties"]
            .__setitem__("titles", True),
        "properties is a list": lambda s: s["properties"]["contacts"]
            .__setitem__("properties", ["titles"]),
        "description is not a string": lambda s: s["properties"]["contacts"]["properties"]
            .__setitem__("seniorities", {"description": ["a"]}),
        "$defs is not a dict": lambda s: s.__setitem__("$defs", []),
        "self-referential $ref": lambda s: s.__setitem__(
            "$defs", {"loop": {"type": "object",
                               "properties": {"again": {"$ref": "#/$defs/loop"}}}}),
    }.items():
        import copy

        schema = copy.deepcopy(real)
        mutate(schema)
        # Must not raise. Content is best-effort; survival is the contract.
        _section_shapes_layer(schema)


def test_a_schema_declaring_no_section_internals_raises():
    """Tolerating an odd node is right; tolerating an empty vocabulary is not.

    With nothing declared the layer renders "internals not yet specified" five
    times, which reads as permission to invent — and inventing section internals
    is exactly what this layer exists to stop, since the scanner accepts
    unrecognised keys and then does nothing. Same rule as an empty key list.
    """
    import copy

    import pytest

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    for section in ("geography", "discovery", "validation", "contacts", "scoring"):
        schema["properties"].pop(section, None)
    with pytest.raises(ValueError, match="declares no internals for section"):
        _section_shapes_layer(schema)


def test_an_unratified_section_without_pinned_knobs_raises_per_section():
    """The guard that existed before #30 fired only when EVERY section was empty.

    That threshold is why it was silent for the one unratified section we
    actually shipped: `scoring` alone rendered "author what the vertical needs"
    while the other four were declared, so `declared` stayed non-zero and the
    all-empty check never ran. The reasoning was right and the threshold was
    wrong — so the guard is asserted PER SECTION here.

    Mutation-checked at the CALL SITE, not on a helper: delete the `raise` in
    `_section_shapes_layer` and this goes red.
    """
    import copy

    import pytest

    from app.skill_builder.prompt import _section_shapes_layer

    # `contacts` is declared today; strip only its internals so the section is
    # present and well formed but carries no shape — the exact `scoring` case,
    # on a section with no pinned knob list to fall back to.
    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["contacts"].pop("properties", None)
    with pytest.raises(ValueError, match="contacts"):
        _section_shapes_layer(schema)


def test_agent_identity_ratifies_the_operator_action_vocabulary():
    """The two free-text operator actions are TAUGHT, not merely tolerated.

    Ratified with aeo-frontend in thread #24. They have three structured
    controls; only two reach us as text, and until this landed the prompt said
    nothing about either — so the model's handling was "it will probably
    understand", which is the failure mode this feature keeps producing: works
    in every demo, fails intermittently in production, no error anywhere.

    Asserting the exact strings here is the point. They live in the frontend's
    code too, and a vocabulary taught in one repo and validated in none is the
    pattern behind most of the cross-repo defects on this feature.
    """
    from app.skill_builder.prompt import AGENT_IDENTITY

    assert "Don't reuse that skill — build a new one." in AGENT_IDENTITY
    assert "For the <Section> section: <note>" in AGENT_IDENTITY


def test_prompt_forbids_inferring_acceptance_from_operator_text():
    """Acceptance is structural, and the prompt must say so.

    `BuilderState.next_open_phase()` reads ONLY the acceptance map, so a
    section's acceptance is decided by the gateway's flag and never by what the
    operator typed. Without this instruction a model could announce a section
    accepted on the strength of a sentence — plausible, unfalsifiable from the
    transcript, and diverging from the state that actually gates finalize.
    """
    from app.skill_builder.prompt import AGENT_IDENTITY

    assert "ACCEPTANCE IS NOT A MESSAGE" in AGENT_IDENTITY
    assert "never as text" in AGENT_IDENTITY


def test_identity_states_that_narrating_an_edit_does_not_perform_it():
    """#27 turn 10: the model wrote a correct diagnosis of the inline
    `{context_ref:…}` defect, said it had fixed it, and then chose
    `request_test_run` — so nothing was written and the operator read a repair
    that never happened. No error on either side.

    A turn carries ONE action, and nothing told the model that. Pinned here
    because it lives in the CACHED prefix (`AGENT_IDENTITY`) rather than a new
    layer, which would have moved the `split(stable_layers=5)` boundary.
    """
    from app.skill_builder.prompt import AGENT_IDENTITY

    assert "exactly ONE action" in AGENT_IDENTITY
    assert "describing an edit does not perform it" in AGENT_IDENTITY
    # The actionable half: say what to do INSTEAD, not just what goes wrong.
    assert "propose_section" in AGENT_IDENTITY
    assert "NEXT turn" in AGENT_IDENTITY


def _rendered():
    from app.skill_builder import contracts
    from app.skill_builder.context import CustomerContext
    from app.skill_builder.prompt import compose

    return compose(
        customer_context=CustomerContext({"organization_name": "Nashville HVAC"}),
        task="Propose the geography section.",
        tools=contracts.tool_schemas(),
        context_field_keys=contracts.context_field_keys(),
        config_positions=contracts.config_positions(),
        runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema(),
    ).render()


def test_prompt_shows_no_worked_example_of_a_literal_default():
    """#28: on a NASHVILLE org the model bound geography with AUSTIN defaults,
    systematically, on fresh sessions.

    It was not inventing the shape — we taught it. The prompt rendered
    `{"context_ref": "excluded_markets", "default": ["Reno"]}` as a worked
    example, and a concrete example is the strongest signal in a prompt: the
    model matched the structure and filled the city from the only other literal
    in scope. Asserting the EXAMPLE is absent, not merely that a rule exists —
    the rule and the counter-example coexisted before, and the example won.
    """
    p = _rendered()
    assert "Reno" not in p
    assert '"default":' not in p, "no worked example may demonstrate a literal default"


def test_prompt_tells_the_model_to_omit_default_and_says_why():
    """A bare prohibition gets weighed against the schema, which permits
    `default`. The reason is what makes it hold: the skill is reused across a
    vertical, so a literal default is one org's data applied to others."""
    p = _rendered()
    assert "Do NOT author a `default`" in p
    assert "VERTICAL" in p
    # The asymmetry is the actual argument — loud failure beats quiet wrongness.
    assert "fails loudly" in p


def test_scoring_is_now_DERIVED_and_the_pinned_fallback_is_gone():
    """#32 landed: backend declared `scoring`'s properties, so the copy dies.

    `_UNRATIFIED_SECTION_KNOBS` held that vocabulary for exactly one day and went
    stale inside four hours — the engine began reading `factors` the same
    afternoon it was pinned, and our drift test structurally could not notice
    because there was no declared property to differ from. The derived path
    cannot drift, which is the entire point of the exchange that removed it.

    Asserted on the RENDERED line rather than on the constant: an empty dict
    proves nothing about what the model is handed.
    """
    from app.skill_builder.prompt import _UNRATIFIED_SECTION_KNOBS, _section_shapes_layer

    assert _UNRATIFIED_SECTION_KNOBS == {}, "the fallback should be empty once declared"

    rendered = _section_shapes_layer(contracts.config_schema())
    scoring = rendered[rendered.index("  scoring:"):]
    # The entry shape must survive derivation — `_type_hint` expanding an array's
    # `items` is what makes that true; without it this reads "list of object".
    #
    # Asserted as membership, NOT as the literal ordered prefix this used to pin
    # ("list of {name, weight, min?, max?"). That form broke on the 2026-08-25
    # re-pin purely because the gateway inserted `key?` and `source_field?`
    # between `name` and `weight` — a schema change this test has no business
    # failing on. Order is the gateway's to choose; expansion is ours to prove.
    assert "list of object" not in scoring, "the array's items were not expanded"
    assert "list of {" in scoring
    for key in ("name", "weight", "min?", "max?", "description?"):
        assert key in scoring, f"{key} missing from the derived factors shape"
    # And the key OUR proposed fragment omitted, which backend caught.
    assert "disqualify_below" in scoring
    assert "not yet specified" not in rendered

    # The 2026-08-25 graded-scoring vocabulary, so this test also proves the pin
    # is current: a stale stub renders none of these and every new skill would be
    # authored presence-only with no ladder.
    for key in ("factors_max", "priority_bands", "disqualify_rules"):
        assert key in scoring, f"{key} missing — stub is pre-2026-08-25"
    for key in ("tiers?", "keywords?", "source_field?"):
        assert key in scoring, f"{key} missing from the derived factors shape"


def test_an_undeclared_section_now_raises_rather_than_using_a_stale_pin():
    """With the fallback empty, the per-section guard is the only path left.

    That is deliberate. A future unratified section should fail LOUDLY here
    rather than quietly render whatever a months-old transcription happened to
    say — the failure mode #30 produced, and the reason the copy was deleted
    rather than merely emptied of `scoring`.
    """
    import copy

    import pytest

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["scoring"].pop("properties", None)
    schema["properties"]["scoring"].pop("allOf", None)
    with pytest.raises(ValueError, match="scoring"):
        _section_shapes_layer(schema)


def test_the_factors_validation_RULE_survives_truncation_into_the_prompt():
    """The rule that stops the builder authoring a config the finalize gate rejects
    must reach the MODEL, not merely exist in the schema.

    It did not, for a while, and nothing noticed. Backend restated
    `scoring.factors`'s description as a hard validation rule with a trigger list;
    the description was 1,684 chars, `_NOTE_BUDGET` is 400, and the rule sat at
    offset 1133 — so it was truncated away in full, along with `tiers` at 573 and
    the earlier advisory wording. The model had never been taught this in ANY
    framing, and the operator was pasting our reject message back into the
    conversation every time as the message bus.

    Re-pinning alone would not have fixed that and would have looked like the pin
    mechanism failing. Backend reordered to put the rule first (536 chars, 342
    surviving) and this asserts the OUTCOME rather than the pin: a future reorder
    that buries the rule again fails here instead of shipping quietly.

    Asserted on the composed layer, because `_notes` in isolation proves nothing
    about what the model is handed.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(contracts.config_schema())
    scoring = rendered[rendered.index("  scoring:"):]

    # The rule itself, and that it is framed as a rule rather than a suggestion.
    #
    # Asserted on "RULE" and "REJECTED" rather than the full phrase this once
    # pinned ("VALIDATION RULE"). Backend compressed the preamble to "RULE, not
    # advice" on 2026-08-25 to buy room for the tier-key clause, and this failed on
    # their wording rather than on anything being wrong -- the same mistake as the
    # ordered-prefix assertion further up this file. Their phrasing is theirs to
    # choose; what must hold is that the framing is imperative and names the
    # consequence.
    assert "RULE" in scoring, "the rule was truncated out of the prompt"
    assert "REJECTED" in scoring, "the consequence is not stated"

    # The trigger list, first and last entries -- a partial list is worse than none,
    # because the model would infer the omitted ones are safe.
    assert "footage" in scoring, "trigger list truncated at its head"
    assert "rooms" in scoring, "trigger list truncated at its tail"

    # The bound-field clause. This is the part that made the loop feel arbitrary:
    # `growth_trajectory` matches no hint by NAME and tripped on the numeric field
    # it binds to. Backend deliberately put it inside sentence 1 because as its own
    # sentence it was the first thing truncation took (measured cut at 394/400).
    assert "source_field" in scoring, "the bound-field clause was truncated"

    # The remedies, as literal SHAPES rather than bare key names. Naming the rule
    # without the shape is what produced the 2026-08-25 rounds: v26 delivered the
    # rule and the model authored `keywords: [...]`; v27 delivered the shapes.
    for remedy in ("`tiers`", "[{threshold,points}]", "`keywords`", "{word:points}"):
        assert remedy in scoring, f"remedy {remedy} truncated out of the prompt"

    # `min` must NOT be offered as a grading option beside the tier shape. That
    # adjacency is what had the model authoring tier rows as {min, max, points}:
    # it read three grading choices in one breath and combined two. Backend moved
    # `min` into a sentence that says where it DOES belong, so the prompt must
    # still mention it -- as a factor-level key, not a grading remedy.
    assert "factor-level" in scoring, (
        "the sentence placing `min`/`max` at factor level was truncated -- without "
        "it `min` reads as a third grading option again"
    )
    assert "those keys ONLY" in scoring, (
        "the additionalProperties:false statement was truncated"
    )

    # And the structural form of the same guarantee, which is stronger than the
    # probes above: backend shortened the description to 342 chars so it sits
    # ENTIRELY under the 400 budget and is never truncated at all. So the rendered
    # note must equal the description verbatim. That catches a future lengthening
    # past the budget even if it keeps every string this test happens to probe for
    # -- which is exactly how the rule got buried the first time, at offset 1133 of
    # 1,684 with `tiers` also cut and nobody noticing.
    from app.skill_builder.prompt import _NOTE_BUDGET, _notes

    factors = contracts.config_schema()["properties"]["scoring"]["properties"]["factors"]
    declared = " ".join(factors["description"].split())
    assert len(declared) <= _NOTE_BUDGET, (
        f"the factors description is {len(declared)} chars against a {_NOTE_BUDGET} "
        "budget, so it is being truncated again -- reorder it (owner's job, see the "
        "_NOTE_BUDGET docblock), do not raise the budget"
    )
    assert _notes(factors, "")[0] == declared, "rendered note is not the description verbatim"


def test_the_scoring_budget_remedies_survive_truncation_into_the_prompt():
    """The two sentences that prevent the axis-budget defect must reach the MODEL.

    Backend shipped a lint rule for "the axis budgets sum over `score_cap`" on
    2026-08-28. We measured that the sentence preventing it was already written
    in the schema and was being eaten HERE: `scoring.factors_max` was 552 chars
    against a 400 budget with sentence boundaries at 44/117/385/488, so
    *"Set this to `score_cap` and every other axis to 0"* -- the general form of
    the remedy -- never rendered. What survived named `region_bonus` and
    `pipeline` as its two worked examples and omitted `multi_source`, which is
    one of the two defaults that actually causes the defect.

    `scoring.priority_bands` failed the same way by EIGHT characters: 408 against
    400, losing the contiguous-ranges rule entirely.

    Backend reordered both (`20c05f3`) rather than us raising the budget, per the
    rule in the `_NOTE_BUDGET` docblock. This asserts the OUTCOME, not the pin:
    unlike the `factors` test above, `factors_max` is 689 chars and IS truncated
    on purpose, so verbatim equality is the wrong assertion -- what must hold is
    that the remedy lands inside the cut.

    Asserted on the composed layer, because `_notes` in isolation proves nothing
    about what the model is handed.
    """
    import re

    from app.skill_builder.prompt import _NOTE_BUDGET, _notes, _section_shapes_layer

    schema = contracts.config_schema()
    props = schema["properties"]["scoring"]["properties"]
    rendered = _section_shapes_layer(schema)
    scoring = rendered[rendered.index("  scoring:"):]

    # -- factors_max: the remedy must land inside the cut.
    note = _notes(props["factors_max"], "")[0]
    assert len(note) <= _NOTE_BUDGET, "the renderer overran its own budget"
    assert note in scoring, "the note is not what the composed layer carries"

    # All FIVE defaults. This IS the structural proof that the remedy sentence
    # survived -- it is the only sentence carrying them -- so no separate probe
    # for its wording is needed, and that matters: the test above this one
    # records being burned TWICE by pinning backend's phrasing, and a bare
    # `"every other axis to 0" in scoring` would be a third.
    #
    # A partial list is worse than none, because the model would infer the
    # omitted axes are zero, which is the defect. The axis names are config keys
    # and the numbers are engine facts (`_DEFAULT_SCORING` in the scanner), so
    # both are contract; only the separator between them is prose, hence \W{0,3}
    # rather than a literal space.
    for axis, default in (
        ("fit", 25),
        ("pipeline", 30),
        ("completeness", 15),
        ("region_bonus", 10),
        ("multi_source", 10),
    ):
        assert re.search(rf"{re.escape(axis)}\W{{0,3}}{default}\b", note), (
            f"the {axis} default ({default}) is not in the rendered note -- either "
            "truncated, or the list was shortened. multi_source was the one missing "
            "before 20c05f3 and it is one of the two that cause the finding"
        )

    # -- priority_bands: the contiguous-ranges rule, lost by 8 chars before 20c05f3.
    bands = _notes(props["priority_bands"], "")[0]
    assert len(bands) <= _NOTE_BUDGET, "the renderer overran its own budget"
    assert re.search(r"no gaps|contiguous", bands), (
        "the contiguous-ranges rule was truncated again -- reorder it (owner's job, "
        "see the _NOTE_BUDGET docblock), do not raise the budget"
    )
    assert "score_cap" in bands, "the rule no longer says what the ranges must cover"

def test_the_gated_model_keys_reach_the_authoring_prompt():
    """The whole point of `_MAX_NESTING_DEPTH = 2`, asserted on the SHIPPED pin.

    These four keys sit two levels below `scoring` and rendered to nothing before
    2026-08-31. `state_aliases` is the one that matters most: the org writes
    "North Carolina", every prospect carries "NC", nothing matches, and no lead is
    ever in market with nothing thrown. It is a SEMANTIC rule, so the validator's
    shape-error path cannot teach it either — `_SHAPE_VALIDATORS` only fires on
    `oneOf` / `required` / `additionalProperties`, and a gate that admits nobody is
    structurally valid. The prompt is the only place it can be taught, which is why
    a re-pin that silently dropped it would be invisible everywhere else.

    Asserts KEYS, not backend's wording: this file has been burned three times by
    literal-string assertions failing on a harmless reword.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(contracts.config_schema())
    for key in (
        "state_aliases",          # gate.target_market.* — the silent-failure trap
        "allowed_states_from",
        "window_stages",          # gate.buying_window.*
        "signal_freshness_months",
    ):
        assert key in rendered, f"{key} no longer reaches the authoring prompt"


def test_the_four_bonus_bands_are_named_properties_not_an_array():
    """Backend keyed bands by DISPLAY NAME until 2026-08-31 — a case-sensitive
    string match with a space in it, validated by nothing. `Company Size` for
    `Company size` scored that band 0; all four mis-cased cost 14 points (98 -> 84)
    with the lane still `qualified`, nothing thrown and nothing logged.

    Named properties make a mis-spelling an authoring error instead. Pinned here
    because the array shape reappearing would restore the silent class AND put the
    band semantics back in one description that cannot fit (measured at 1,439 chars
    against a 400 budget, 25.7% rendered).
    """
    bonus = contracts.config_schema()["properties"]["scoring"]["properties"]["bonus"]
    props = bonus.get("properties") or {}
    assert "bands" not in props, "the retired array shape is back; see the docblock"
    for band in ("signal_strength", "company_size", "confirmed_contact", "signal_recency"):
        assert band in props, f"{band} is not a declared property of `bonus`"

def test_a_forbidden_key_is_never_expanded():
    """Found by the audit pass over the depth-2 change, not by a failing test.

    At depth 1 the question could not arise. At depth 2 it can: `_nested_object_lines`
    marks a `not: {}` child "NEVER author this", and the recursion would then render
    that child's own properties — teaching the model how to author the very thing
    the line above forbids, which is worse than silence because it reads as a
    specification.

    Unreachable on today's schema: the only `not: {}` node
    (`$defs.discoverySource.seed_firms`) declares no properties. Pinned against a
    synthetic node that does, because "unreachable today" is a fact about the schema
    and not about our renderer.

    ⚠️ The forbidden node must sit ONE LEVEL BELOW a section property, which is where
    the marker logic lives. My first version of this test put it at section-property
    level and failed with the guard in place — `_section_shapes_layer`'s own loop has
    no `not: {}` handling at all, so a forbidden SECTION property is never marked.
    That gap is pre-existing and unreachable (no such node exists); recorded here
    rather than fixed, so whoever adds one finds the reason.
    """
    import copy

    from app.skill_builder.prompt import _section_shapes_layer

    schema = copy.deepcopy(contracts.config_schema())
    schema["properties"]["contacts"]["properties"]["wrapper"] = {
        "type": "object",
        "properties": {
            "banned": {
                "not": {},
                "type": "object",
                "properties": {
                    "secret_knob": {
                        "type": "string",
                        "description": "SHOULD NEVER RENDER.",
                    }
                },
            }
        },
    }
    rendered = _section_shapes_layer(schema)
    assert "banned — NEVER author this" in rendered
    assert "secret_knob" not in rendered
    assert "SHOULD NEVER RENDER." not in rendered

def test_the_gated_default_and_its_trigger_survive_the_note_budget():
    """`gated` became the DEFAULT for new skills on 2026-08-31, with a trigger.

    Backend's first attempt at this steered the OTHER way — *"Omit or `legacy` …
    that path is unchanged and is what every existing skill uses"* — and a real
    authoring session read it and chose legacy, which we all scored as a PASS
    against a criterion that only asked whether the retired `bands` array appeared.
    It was a fail against what the product wanted.

    So two things are pinned, and neither is backend's wording:

    - `default` is the contract's own declaration, not prose.
    - The note must render WHOLE. A trigger truncated at a sentence boundary is
      the `_NOTE_BUDGET` failure this repo has hit four times, and here it would
      silently restore the old behaviour: the model would read what `gated` IS and
      never reach the condition telling it when to author one.

    Asserts `signal_date` because that is the field the condition keys on — a
    contract token the engine reads, which survives a reword.
    """
    from app.skill_builder.prompt import _notes

    model = contracts.config_schema()["properties"]["scoring"]["properties"]["model"]
    assert model.get("default") == "gated"

    raw = " ".join(str(model["description"]).split())
    rendered = _notes(model, "")[0]
    assert rendered == raw, (
        f"the `model` trigger is truncated at {len(rendered)} of {len(raw)} chars — "
        "the condition telling the builder WHEN to author a gate would not reach it"
    )
    assert "signal_date" in rendered

def test_the_partial_lane_bases_survive_the_SHAPE_error_budget_too():
    """`scoring.partial` must render whole at BOTH budgets, not just the prompt's.

    Backend's first version said "keep every ceiling below `floor`". A real gated
    config then authored `target_market_only` base 40 / ceiling 65 — both ceilings
    genuinely below 80, and their own lint passed it with 0 issues. But a lane
    scores `min(base + bonus.max, ceiling)`, so base 40 with a 20-point bonus
    reaches 60 and lands inside 46-79, the band the whole model exists to keep
    structurally empty. **A ceiling constraint cannot express a base constraint.**

    The fix is text: the bases are now stated as fixed values with the `min()`
    arithmetic that explains why. So the thing to pin is not the wording but that
    the sentence carrying the arithmetic SURVIVES — and at 340, not 400, because
    `_SHAPE_DESCRIPTION_BUDGET` is the tighter of the two and a cut there would
    drop the arithmetic while leaving the reassuring first sentence intact,
    restoring exactly the latitude that produced 40 and 30.
    """
    from app.skill_builder.prompt import _notes
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    node = contracts.config_schema()["properties"]["scoring"]["properties"]["partial"]
    raw = " ".join(str(node["description"]).split())

    assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET, (
        f"`partial` is {len(raw)} chars against the {_SHAPE_DESCRIPTION_BUDGET}-char shape-error "
        "budget: the min() arithmetic would be cut at failure time"
    )
    assert _notes(node, "")[0] == raw
    assert "min(base" in raw, "the arithmetic that makes a ceiling-only rule insufficient is gone"

def test_the_literal_allow_list_the_gate_reads_is_declared_and_renders_whole():
    """`allowed_states` is the field whose ABSENCE produced a dead gate.

    Until backend's `8c41783` it was not a declared property at all, so the shapes
    layer could not teach it and the model could not author it. What the model
    could see was `allowed_states_from` — and backend's own lint told the author to
    *"declare `allowed_states_from`"*. It did exactly that. Nothing in either repo
    dereferences `_from`; `in_target_market` reads the LITERAL and returns False on
    an empty set, so **every lead failed G1**, nothing reached `floor`, and the book
    capped at a partial ceiling with no error anywhere.

    That is the same mechanism measured earlier in this workstream: the model
    authors the declared shape, and only the declared shape. An undeclared key is
    not a key the model omits — it is one it cannot write.

    Pinned at 340 as well as 400: this description carries the distinction between
    the literal and the `_from` pointer, and a cut restores the exact ambiguity that
    produced the dead gate.
    """
    from app.skill_builder.prompt import _notes, _section_shapes_layer
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    tm = (contracts.config_schema()["properties"]["scoring"]["properties"]
          ["gate"]["properties"]["target_market"]["properties"])
    assert "allowed_states" in tm, "the literal allow-list the gate reads is not declared"

    raw = " ".join(str(tm["allowed_states"]["description"]).split())
    assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET
    assert _notes(tm["allowed_states"], "")[0] == raw

    # and it must actually reach the model, which is what half A made possible
    assert "allowed_states" in _section_shapes_layer(contracts.config_schema())

#: The gate keys the model must be able to author, and that a re-pin must not
#: un-declare. Each earned its place by being MISSING and producing a silent
#: defect, so this list is a scar record rather than a schema summary.
_GATE_KEYS_THE_MODEL_MUST_BE_ABLE_TO_AUTHOR = (
    ("target_market", "allowed_states"),   # absent -> every lead fails G1, book caps
    ("target_market", "exclude_rules"),    # absent -> org disqualifiers never applied
)


def test_every_gate_key_the_engine_reads_is_declared_and_reaches_the_model():
    """Generalises the `allowed_states` pin instead of adding one test per scar.

    Backend applied our correction — *an undeclared key is not one the model omits,
    it is one it cannot write* — as a PREDICATE (`gate-key-parity.py`, declared vs
    read per gate sub-object) and it immediately returned the same defect one
    property over: `exclude_rules` was read at `gated_score.py:117` and undeclared,
    while `exclude_rules_from` was declared and dereferenced by nothing. A session
    authored three keys that do nothing and could not author the one that works.

    So the assertion is per-key rather than per-incident: declared, and reaching the
    composed layer. A key that is declared but not rendered is the same defect with
    an extra step, which is why both halves are checked.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    schema = contracts.config_schema()
    gate = schema["properties"]["scoring"]["properties"]["gate"]["properties"]
    rendered = _section_shapes_layer(schema)
    for parent, key in _GATE_KEYS_THE_MODEL_MUST_BE_ABLE_TO_AUTHOR:
        assert key in gate[parent]["properties"], (
            f"gate.{parent}.{key} is not declared — the model cannot author it"
        )
        assert key in rendered, f"gate.{parent}.{key} is declared but never reaches the model"

def test_company_size_field_keeps_the_sentence_that_names_the_silent_failure():
    """`bonus.company_size.field` is the only one of the three band `field` keys the
    engine actually READS — `band_company_size` takes it off the lead, while
    `band_recency` and `band_signal_strength` hardcode `sig.signal_date` and
    `sig.signal_class`.

    So this is the one whose misuse is silent and costly. A skill declared four
    tiers over `employee_count` while its discovery sources collected only
    `company_name, website, location, industry, products_services`. **`unknown: 2`
    is what hides it**: the band degrades to a flat midpoint rather than a zero, so
    a 10-person shop and a 500-person manufacturer score identically and nothing
    looks broken.

    Backend trimmed this description 346 -> 311 when their budget check — the one
    that now tests both of our budgets, after we reported the 340 — caught it six
    characters over. Six characters would have dropped the last sentence, which is
    the one carrying the warning. Pinned at 340 for that reason, not at 400.
    """
    from app.skill_builder.prompt import _notes
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    node = (contracts.config_schema()["properties"]["scoring"]["properties"]
            ["bonus"]["properties"]["company_size"]["properties"]["field"])
    raw = " ".join(str(node["description"]).split())
    assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET, (
        f"{len(raw)} chars against the {_SHAPE_DESCRIPTION_BUDGET}-char shape-error budget"
    )
    assert _notes(node, "")[0] == raw

    # A budget assertion ALONE is vacuous here, and mutation proved it: the previous
    # 60-char description also rendered whole, so "renders whole" passed against the
    # very state this test exists to prevent. `unknown` is the sibling key whose
    # midpoint hides the failure, so naming it is the contract token that
    # distinguishes a description that warns from one that merely fits.
    assert "unknown" in raw, (
        "the description no longer names `unknown`, the sibling whose midpoint makes "
        "an uncollected field look like a working band"
    )

#: The gated model's three fixed numbers, and the value each must state.
#:
#: 🔴 UPDATED 2026-09-01. Two of the three are now `const` in the schema
#: (`floor: 80`, `bonus.max: 20`), so a `floor: 70` config is structurally INVALID
#: rather than merely discouraged. Backend added them after we pointed out that all
#: three carried `minimum: 1` and nothing else, so every version of the invariant
#: had lived in prose plus a lint that runs after the fact.
#:
#: `score_cap` deliberately stayed prose-only: **20 legacy configs declare it**, and
#: a `const` would forbid a legacy skill from ever choosing another cap — a
#: narrowing the change had no mandate for. `floor` and `bonus.max` are gated-only
#: in every config that exists, which is what made those two safe.
_GATED_CONST_NUMBERS = (("floor",), ("bonus", "max"))
_GATED_FIXED_NUMBERS = (("floor", "80"), ("score_cap", "100"), (("bonus", "max"), "20"))


def test_the_three_fixed_gated_numbers_state_their_value_and_render_whole():
    """`floor` said *"set with `score_cap` so the gap equals `bonus.max` — floor 80
    and cap 100 leaves exactly 20 points"*. A skill authored **floor 70, bonus.max
    30, cap 100** — which satisfies that sentence exactly, because `100 - 70 = 30`
    and the 80 reads as an illustration. Its empty band became 46-69 while every
    other skill's is 46-79, so a 75 means "qualified" in one vertical and
    "impossible, therefore a bug" in another.

    Third instance of one shape in this thread: a rule that is true, insufficient,
    and followed into a defect — after `min(base + bonus, ceiling)` read as
    "ceilings below floor", and `allowed_states_from` read as the allow-list.

    Asserts the VALUE, not the wording: 80 / 100 / 20 are the contract's own numbers
    and survive a reword. And whole at 340, because these three now sit under that
    budget and a cut would drop the word FIXED while leaving the arithmetic that was
    already misread once.
    """
    from app.skill_builder.prompt import _notes
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    sc = contracts.config_schema()["properties"]["scoring"]["properties"]

    # The structural half: prose is now the SECOND line of defence, not the only one.
    # A re-pin that dropped a `const` would leave every prose assertion below still
    # passing, which is precisely the shape of defect this thread kept finding.
    for path in _GATED_CONST_NUMBERS:
        node = sc[path[0]]["properties"][path[1]] if len(path) == 2 else sc[path[0]]
        label = ".".join(path)
        assert "const" in node, (
            f"`{label}` lost its `const` — the invariant is back to prose only, and a "
            "config violating it would validate"
        )

    for path, value in _GATED_FIXED_NUMBERS:
        node = sc[path[0]]["properties"][path[1]] if isinstance(path, tuple) else sc[path]
        label = ".".join(path) if isinstance(path, tuple) else path
        raw = " ".join(str(node["description"]).split())
        assert value in raw, f"`{label}` no longer states its fixed value {value}"
        assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET, (
            f"`{label}` is {len(raw)} chars against the {_SHAPE_DESCRIPTION_BUDGET}-char "
            "shape-error budget; the FIXED marker would be cut"
        )
        assert _notes(node, "")[0] == raw

def test_the_buying_window_defaults_name_their_value_not_just_their_semantics():
    """A description can be complete about MEANING and silent about VALUE, and the
    silence is what the model acts on.

    `signal_freshness_months` described the comparison correctly and never named a
    number: *"compared STRICTLY"*, *"deliberately stricter"*, *"strict about
    admitting"* — three uses of the word, no value, while the engine's own fallback
    is 18 and the live skill uses 18. Two unrelated verticals then authored
    IDENTICAL parameters — 3-of-5 stages, 6 months — which is the signature of
    schema-driven authoring rather than data-driven. Scored against a real 159-lead
    book it qualified **5** leads where the live config qualifies **35**.

    🔑 And the decomposition matters more than the total, because it is
    counter-intuitive: `window_stages` is the dominant term. Dropping the two
    earliest stages cost **23 of the 30 lost leads (77%)**; the 3x narrower window
    cost the other 7. The gate's real filter is the fresh signal, so most qualified
    leads sit in EARLY stages carrying recent signals — exactly the ones an "active
    buying window" reading excludes.

    ⚠️ Distinct from the six failures before it, and `const` cannot fix it: 6 months
    is legitimate for an org that states it. Only naming the default and pricing the
    deviation can. So the pin is that the default is NAMED — 18 is the engine's
    fallback, a contract value, not backend's phrasing.
    """
    from app.skill_builder.prompt import _notes
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    bw = (contracts.config_schema()["properties"]["scoring"]["properties"]
          ["gate"]["properties"]["buying_window"]["properties"])

    freshness = " ".join(str(bw["signal_freshness_months"]["description"]).split())
    assert "18" in freshness, (
        "`signal_freshness_months` no longer names 18, the engine's fallback — a "
        "description that is all semantics and no value steers the model small"
    )
    for key in ("signal_freshness_months", "window_stages"):
        raw = " ".join(str(bw[key]["description"]).split())
        assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET, f"`{key}` is {len(raw)} chars"
        assert _notes(bw[key], "")[0] == raw

def test_priority_bands_carries_both_of_its_rules_at_the_tighter_budget():
    """`priority_bands` was 408 chars and losing its NO-GAPS rule by eight
    characters at 400 — while one of backend's live-config tests pinned that same
    rule separately, so the test passed while the sentence never reached the model.

    🔑 That is the vacuous-assertion shape again, from the other side: **a
    description already over budget is invisible to a pin that only watches for the
    truncated set to GROW.** It is the same class as the `company_size.field`
    assertion I shipped and mutation caught.

    The rewrite also fixes a defect that was arithmetic, not style. A config shipped
    `16-30 "Active Signal, Outside Territory"`, which labelled **92 in-territory
    prospects "Outside Territory"** on a customer-facing list, crediting them with a
    signal they did not have. `priority_band(score, bands)` is a numeric lookup with
    no access to the lane, and the lanes are not separable by score anyway —
    `signal_only` spans 10-30 and `target_market_only` 25-45, so a target-market lead
    with a small bonus lands *inside the band named for the other lane*. **No ladder
    can distinguish them, so no label should try.**

    Pinned at 340 rather than 400 because that is the budget the no-gaps rule was
    already failing, and both rules now have to survive it.
    """
    from app.skill_builder.prompt import _notes
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    node = contracts.config_schema()["properties"]["scoring"]["properties"]["priority_bands"]
    raw = " ".join(str(node["description"]).split())
    assert len(raw) <= _SHAPE_DESCRIPTION_BUDGET, (
        f"`priority_bands` is {len(raw)} chars against the {_SHAPE_DESCRIPTION_BUDGET}-char "
        "shape-error budget — one of its two rules is being cut"
    )
    assert _notes(node, "")[0] == raw

def test_a_bound_key_renders_its_BINDING_hint_not_just_its_name():
    """Closes the blind spot in `test_every_gate_key_the_engine_reads_...`.

    That test asserts the key REACHES the model and says nothing about the hint
    beside it — so when `allowed_states` and `exclude_rules` moved from
    `type: array` to `allOf: [{$ref: boundStringList}]`, they began rendering as
    bare `value` and the suite stayed green. `contacts.titles`, a bare `$ref` to the
    SAME `$def`, rendered `{"context_ref": "<key>"} or a list of strings` throughout.

    🔑 Backend's `allOf` is CORRECT, not a workaround: draft-07 ignores siblings of a
    bare `$ref`, so `{"$ref": ..., "description": ...}` loses the description
    silently. Keeping both requires `allOf`. The gap was ours — `_type_hint`
    understood one spelling of a reference and not the other.

    Third instance in this file of asserting a property the failing case also
    satisfies: the key was present, the hint was useless, and only rendering the
    line and reading it showed the difference.
    """
    from app.skill_builder.prompt import _section_shapes_layer

    layer = _section_shapes_layer(contracts.config_schema())

    def line_for(key):
        line = next((l.strip() for l in layer.splitlines()
                     if l.strip().startswith(key + " ")), None)
        assert line is not None, f"{key} does not reach the model at all"
        return line

    for key in ("allowed_states", "titles"):
        line = line_for(key)
        assert "context_ref" in line, (
            f"`{key}` renders as {line!r} — the binding form is not being taught, so "
            "the model cannot know it may bind instead of hard-coding a literal"
        )

    # 🔴 `exclude_rules` is DELIBERATELY array-only, and the asymmetry is the point.
    #
    # Backend opened it to `context_ref` alongside `allowed_states` on the symmetry,
    # without checking what its key RESOLVES to. `home_markets` resolves to a list;
    # `disqualifiers` resolves to the org's free prose. `in_target_market` does
    # `for rule in exclude_rules`, which over a string yields single CHARACTERS —
    # 'S', 'c', 'h', 'o', 'o', 'l' — so every company whose name contains one is
    # excluded and Gate 1 fails for everyone. A finalized skill passed all five gate
    # checks and scored 0 of 159 leads where the live config scores 35.
    #
    # 🔑 The distinction is the RESOLVED TYPE, not the mechanism, which is why
    # `allowed_states` keeps its binding above. Asserting the asymmetry rather than
    # deleting the key is deliberate: "make these two consistent" is exactly the
    # reasoning that caused the defect, so it should fail a test rather than look
    # like tidying.
    assert "context_ref" not in line_for("exclude_rules"), (
        "`exclude_rules` is offering a binding again — `disqualifiers` resolves to "
        "prose, and iterating a string yields characters, which fails Gate 1 for "
        "every lead with no error"
    )

def test_no_declared_key_is_taught_as_a_bare_value():
    """Class-level detector for the `allOf` regression, rather than one more scar.

    `_type_hint` falls back to `subschema.get("type", "value")`, so a rendered hint
    of exactly `value` means the renderer understood neither the type nor the
    reference — the model is told a key accepts "value", which teaches nothing.

    That is what `allowed_states` and `exclude_rules` rendered when backend moved
    them to `allOf: [{$ref: ...}]`, and the suite stayed green because every
    assertion at the time checked that the KEY was present. This asserts the absence
    of the degraded OUTPUT instead, so it catches shapes nobody has written yet — a
    two-element `allOf`, for instance, still degrades to `value` today and no
    per-key test would notice.

    ⚠️ `object` is NOT degraded and is deliberately allowed: it is the correct hint
    for a container whose children `_nested_object_lines` then expands, and 20 keys
    legitimately render it. Asserting against `object` too would fail on landing.
    """
    import re

    from app.skill_builder.prompt import _section_shapes_layer

    layer = _section_shapes_layer(contracts.config_schema())
    useless = [
        m.group(1)
        for line in layer.splitlines()
        if (m := re.match(r"^\s*([a-z_]+)(?:\s*\(required\))?\s+—\s+value\s*$", line))
    ]
    assert not useless, (
        f"these keys are taught as bare `value`, which teaches nothing: {useless}. "
        "Either the schema declares no type, or `_type_hint` does not understand the "
        "reference form used"
    )
