"""draftConfig construction + RFC 6902 deltas (PRD §4/§6)."""

from app.skill_builder import draft, validator
from app.skill_builder.state import PHASES


def test_build_slug_follows_vertical_convention():
    assert draft.build_slug("Auto Parts") == "auto-parts-prospect-scanner"
    assert draft.build_slug("HVAC & Plumbing") == "hvac-plumbing-prospect-scanner"


def test_build_slug_falls_back_when_vertical_missing():
    assert draft.build_slug(None) == "prospect-scanner"
    assert draft.build_slug("   ") == "prospect-scanner"


def test_skeleton_has_all_phase_sections_and_is_valid():
    cfg = draft.skeleton(
        name="ACME Prospect Scanner",
        vertical="auto parts",
        lead_type="B",  # organizations.lead_type ENUM, not descriptive prose
        product_description="desc",
        type_="customer",
    )
    for phase in PHASES:
        assert phase in cfg
    assert validator.is_valid(cfg, require_complete=True)


def test_skeleton_omits_what_is_not_known_rather_than_placeholdering():
    """Ratified authoring rule: omit unknown fields, never placeholder them.

    `type` / `vertical` / `lead_type` are enum- or pattern-constrained under a
    closed root envelope, so `""` or a guessed value is a hard failure from the
    FIRST snapshot — and one that happened to pass would reach finalize looking
    legitimate. Absent is the honest representation of "not decided yet".
    """
    cfg = draft.skeleton(
        name="New Prospect Scanner",
        vertical=None,
        lead_type=None,
        product_description="",
    )
    for absent in ("type", "vertical", "lead_type", "product_description"):
        assert absent not in cfg, f"{absent} should be omitted, not placeholdered"
    # Still valid incrementally — that is the point of omitting.
    assert validator.is_valid(cfg, require_complete=False)


def test_skeleton_drops_a_non_enum_lead_type_rather_than_emitting_it():
    """`lead_type` must be an organizations.lead_type value or absent.

    The gateway's runtime-context returns that enum column directly, so a
    conforming payload can only carry A/B/MIXED or null. But
    `CustomerContext.lead_type` falls back to free-text
    `onboarding_data.lead_type` / `lead_scoring.lead_type`, so prose can still
    reach us. Guarding turns that into an omitted key instead of a config that
    fails the contract enum.
    """
    for prose in ("fleet operators", "B2B", "b", "mixed", ""):
        cfg = draft.skeleton(
            name="X", vertical="auto parts", lead_type=prose, product_description="d"
        )
        assert "lead_type" not in cfg, f"{prose!r} must not be emitted"
        assert validator.is_valid(cfg, require_complete=False)

    for valid in ("A", "B", "MIXED"):
        cfg = draft.skeleton(
            name="X", vertical="auto parts", lead_type=valid, product_description="d"
        )
        assert cfg["lead_type"] == valid


def test_skeleton_carries_the_contract_version_and_no_execution_phases():
    cfg = draft.skeleton(
        name="X", vertical="auto parts", lead_type=None, product_description="d"
    )
    # `version` names the CONTRACT (a `const`), so it is known up front — the one
    # field safe to carry from the first snapshot.
    assert cfg["version"] == "1.0"
    # Runtime concept, stripped by the gateway before we see a config.
    assert "execution_phases" not in cfg
    # Retired keys: the flat `pipeline_stages` and camelCase `productDescription`
    # both now fail the closed envelope.
    assert "pipeline_stages" not in cfg
    assert "productDescription" not in cfg


def test_diff_then_apply_round_trips():
    before = {"a": 1, "geography": {}}
    after = {"a": 1, "geography": {"scope": {"context_ref": "home_markets"}}}
    patch = draft.diff(before, after)
    assert patch  # non-empty RFC 6902 op list
    assert draft.apply(before, patch) == after


def test_apply_does_not_mutate_input():
    before = {"geography": {}}
    patch = [{"op": "add", "path": "/geography/scope", "value": "x"}]
    result = draft.apply(before, patch)
    assert before == {"geography": {}}  # untouched
    assert result == {"geography": {"scope": "x"}}


def test_set_section_returns_new_config_and_patch():
    cfg = draft.skeleton(
        name="ACME", vertical="auto parts", lead_type="B", product_description="d"
    )
    section = {"rules": [{"source": {"context_ref": "lookalike_sources"}}]}
    new_cfg, patch = draft.set_section(cfg, "discovery", section)
    # `max_prospects` is the build-mode ceiling seed (see `_SEEDED_DEFAULTS`).
    expected = {**section, "max_prospects": 150}
    assert new_cfg["discovery"] == expected
    assert cfg["discovery"] == {}  # original untouched
    # The emitted delta, applied to the old config, reproduces the new one.
    assert draft.apply(cfg, patch)["discovery"] == expected


def test_section_wrapped_in_its_own_name_is_unwrapped():
    """A live turn produced `{"geography": {...}}` as the geography BODY.

    That lands as draftConfig.geography.geography and VALIDATES, because
    sections are additionalProperties:true — so validation, the R12 lint, the
    patch and the delta all pass and nothing reports it. Frontend cannot catch
    it either: they capture event types, not delta payloads.
    """
    # `max_discovery_rounds` is authored explicitly so the build-mode seed
    # (see `test_geography_gets_the_default_round_cap*`) does not fire here —
    # this test is about unwrapping and nothing else.
    body = {
        "home_markets": {"context_ref": "home_markets"},
        "targeting": {"max_discovery_rounds": 2},
    }
    cfg, patch = draft.set_section({}, "geography", {"geography": body})

    assert cfg["geography"] == body, "should store the BODY, not the wrapper"
    assert "geography" not in cfg["geography"], "double-nesting survived"
    # The emitted delta is what the gateway applies, so it must carry the
    # unwrapped value too — storing it correctly while emitting the wrapper
    # would desync our draft from theirs.
    assert draft.apply({}, patch)["geography"] == body


def test_unwrap_leaves_legitimate_single_key_sections_alone():
    """The guard must not 'fix' a partial body.

    A section may legitimately carry one key — the discriminator is that the
    key is NOT the phase name. Without this, an over-eager unwrap would
    silently promote an inner object and lose the real body.
    """
    # Authored round cap for the same reason as the test above: keep the seed
    # out of an unwrap assertion.
    partial = {"targeting": {"geo_strictness": "metro", "max_discovery_rounds": 2}}
    cfg, _ = draft.set_section({}, "geography", partial)
    assert cfg["geography"] == partial

    # Same key as the phase but NOT an object → not the wrapping shape. Asserted
    # key-wise because the seed adds a sibling `targeting` to this synthetic body;
    # what matters is that the inner value was NOT promoted.
    scalar = {"geography": "us-west"}
    cfg2, _ = draft.set_section({}, "geography", scalar)
    assert cfg2["geography"]["geography"] == "us-west"


# -- the initial `max_discovery_rounds` proposal (Leo, 2026-08-20) -------------


def test_geography_gets_the_default_round_cap_when_the_model_omits_targeting():
    """The knob has to reach the PROPOSAL, not just the scanner.

    Every `geographyTargeting` knob is optional with a runtime fallback, so an
    unauthored one is invisible: the operator is shown no number and can revise
    nothing. Seeding it puts `max_discovery_rounds` in the STATE_DELTA, which is
    where an operator reviews and changes it.
    """
    cfg, patch = draft.set_section({}, "geography", {"home_markets": ["us-west"]})

    assert cfg["geography"]["targeting"]["max_discovery_rounds"] == 4
    assert draft.INITIAL_MAX_DISCOVERY_ROUNDS == 4
    # It must be in the emitted patch too — a value that exists only in our local
    # copy is a value the gateway never persists and the operator never sees.
    assert draft.apply({}, patch)["geography"]["targeting"]["max_discovery_rounds"] == 4


def test_default_round_cap_fills_an_existing_targeting_object():
    cfg, _ = draft.set_section(
        {}, "geography", {"targeting": {"geo_strictness": "state"}}
    )
    assert cfg["geography"]["targeting"] == {
        "geo_strictness": "state",
        "max_discovery_rounds": 4,
    }


def test_an_authored_round_cap_is_never_overwritten():
    """"Up to the operator if they want to change it" — they change it by saying a
    number, the model authors it, and the seed must leave it alone."""
    cfg, _ = draft.set_section(
        {}, "geography", {"targeting": {"max_discovery_rounds": 2}}
    )
    assert cfg["geography"]["targeting"]["max_discovery_rounds"] == 2


def test_default_round_cap_is_reapplied_on_a_later_geography_revision():
    """The anti-silent-regression property, and the reason this is not gated on
    "the section was empty".

    A section write REPLACES the section, and the model re-proposes from what IT
    authored — it never saw the injected value. So a later, unrelated revision
    (change a market, keep everything else) drops the knob, and a run that the
    operator approved at 4 rounds quietly falls back to the scanner's 2. Absent
    therefore always means the default.
    """
    first, _ = draft.set_section({}, "geography", {"home_markets": ["us-west"]})
    assert first["geography"]["targeting"]["max_discovery_rounds"] == 4

    # A revision that forgets the knob entirely.
    second, _ = draft.set_section(first, "geography", {"home_markets": ["us-east"]})
    assert second["geography"]["targeting"]["max_discovery_rounds"] == 4
    assert second["geography"]["home_markets"] == ["us-east"]


def test_edit_mode_does_not_seed_the_round_cap():
    """An edit session's config came from a finalized skill that is already
    running. Injecting a knob it omits would move a live skill from 2 rounds to 4
    because someone opened its geography section to change a market."""
    cfg, _ = draft.set_section(
        {}, "geography", {"home_markets": ["us-west"]}, edit_mode=True
    )
    assert "targeting" not in cfg["geography"]


def test_the_round_cap_is_geography_only():
    """`max_discovery_rounds` is `geography.targeting`, despite the name reading
    like a discovery knob. Seeding it into `discovery` would author a key the
    engine does not read there — accepted by `additionalProperties: true` and then
    silently ignored."""
    for phase in ("discovery", "validation", "contacts", "scoring"):
        cfg, _ = draft.set_section({}, phase, {"sources": {}})
        assert "targeting" not in cfg[phase], phase


def test_only_geography_and_discovery_are_seeded_at_all():
    """The seed table is the whole blast radius. `validation`, `contacts` and
    `scoring` must come back byte-identical to what the model authored."""
    for phase in ("validation", "contacts", "scoring"):
        section = {"authored": True}
        cfg, _ = draft.set_section({}, phase, section)
        assert cfg[phase] == {"authored": True}, phase


# -- the run's prospect ceiling ----------------------------------------------


def test_discovery_gets_the_prospect_ceiling_when_the_model_omits_it():
    """Omitting `max_prospects` is not "no opinion", it is the unbounded path:
    backend measured it at 82 min / 91% of the 5400s deadline against 57 min /
    64% with the ceiling, and the run that motivated it (0a85e4bd) died at the
    deadline having scored nothing."""
    cfg, patch = draft.set_section({}, "discovery", {"sources": {"a": {}}})

    assert cfg["discovery"]["max_prospects"] == 150
    assert draft.INITIAL_MAX_PROSPECTS == 150
    assert draft.apply({}, patch)["discovery"]["max_prospects"] == 150


def test_an_authored_prospect_ceiling_is_never_overwritten():
    cfg, _ = draft.set_section({}, "discovery", {"max_prospects": 40})
    assert cfg["discovery"]["max_prospects"] == 40


def test_edit_mode_does_not_seed_the_prospect_ceiling():
    cfg, _ = draft.set_section(
        {}, "discovery", {"sources": {"a": {}}}, edit_mode=True
    )
    assert "max_prospects" not in cfg["discovery"]


def test_prospect_ceiling_is_reapplied_on_a_later_discovery_revision():
    first, _ = draft.set_section({}, "discovery", {"sources": {"a": {}}})
    second, _ = draft.set_section(first, "discovery", {"sources": {"b": {}}})
    assert second["discovery"]["max_prospects"] == 150


def test_a_one_level_seed_does_not_mutate_the_section_it_was_given():
    """The regression the two-level geography case could not catch.

    `discovery.max_prospects` has no container to rebuild, so a copy-on-the-way-up
    implementation wrote the seed into the CALLER's dict. Nothing failed: the
    `set_section` assertion compared the result against the very object that had
    been mutated, so the test passed BECAUSE of the bug.
    """
    section = {"sources": {"a": {}}}
    draft.set_section({}, "discovery", section)
    assert section == {"sources": {"a": {}}}


def test_the_seeded_geography_section_still_validates():
    cfg = draft.skeleton(
        name="ACME Prospect Scanner",
        vertical="auto parts",
        lead_type="B",
        product_description="brake and suspension parts distribution",
    )
    cfg, _ = draft.set_section(
        cfg, "geography", {"home_markets": {"context_ref": "home_markets"}}
    )
    assert validator.validate_config(cfg, require_complete=False) == []


def test_set_section_does_not_mutate_the_section_it_was_given():
    """The seed returns copies. A model-supplied body is also carried in the
    decision object the caller still holds, so mutating it in place would make the
    injected value show up in places that never asked for it."""
    section = {"home_markets": ["us-west"]}
    draft.set_section({}, "geography", section)
    assert section == {"home_markets": ["us-west"]}
