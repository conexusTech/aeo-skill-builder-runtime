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


def test_pipeline_is_declared_by_the_schema_and_deliberately_NOT_taught():
    """🔴 The exclusion is the decision. Do not "fix" this by adding `pipeline`.

    The gateway declared six `pipeline` keys on 2026-08-21 (`3169d10`) and asked us to
    re-pin and teach them, on the stated premise that our builder "keeps emitting
    {key, label}". **It emits neither.** `pipeline` appears nowhere in this runtime's
    Python: not in `draft.skeleton()`, not in `_SHAPED_SECTIONS`, nowhere. We have never
    authored a stage vocabulary at all.

    And we cannot, yet, because there is no slot to propose one into:

    * `propose_section` carries a section BODY, never a section NAME -- it applies to
      whichever of the five authoring phases is open (`draft._unwrap_self_named(phase,
      ...)`), so the model cannot direct a proposal at `pipeline`.
    * `agui_state_envelope.json` -- the gateway's own pinned contract -- keys
      `acceptance` by "the five authoring section names (geography, discovery,
      validation, contacts, scoring)". A sixth accepted section is a THREE-repo change,
      not something this module may decide.

    So teaching the shape without a slot is not merely useless, it is the #30 failure
    reproduced exactly: the model, told `pipeline.stages` is authorable, writes the
    stages into whichever phase happens to be open. That lands as
    `draftConfig.scoring.stages`, which VALIDATES (sections are
    `additionalProperties: true`), passes the R12 lint, applies cleanly as a delta, and
    is then read by nobody. Silent, and indistinguishable from a vertical that has no
    ladder -- the same shape as the defect the gateway was fixing when it asked.

    This test therefore pins BOTH halves, because either alone rots:
      1. the schema DOES declare the keys (so the re-pin is real and current), and
      2. we deliberately do NOT teach them (so the gap stays visible until the
         cross-repo decision is made).

    Delete it in the same commit that gives `pipeline` a proposal slot, never before.
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
            f"{key} reached the prompt. If a proposal slot for `pipeline` now exists, "
            "delete this test in that commit; if not, the model has just been taught "
            "to author into a section that will be silently dropped."
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


def test_nested_definitions_expand_one_level_and_no_further():
    """`targeting` renders as a bare 'object' without the nested pass, losing that
    `use_zip_discovery` falsy means SKIP. Bounded at one level so this stays a
    renderer rather than becoming a second jsonschema."""
    from app.skill_builder.prompt import _section_shapes_layer

    rendered = _section_shapes_layer(contracts.config_schema())
    assert "use_zip_discovery" in rendered
    assert "FALSY MEANS SKIP" in rendered


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
    assert "list of {name, weight, min?, max?" in scoring
    # And the key OUR proposed fragment omitted, which backend caught.
    assert "disqualify_below" in scoring
    assert "not yet specified" not in rendered


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
