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
    assert new_cfg["discovery"] == section
    assert cfg["discovery"] == {}  # original untouched
    # The emitted delta, applied to the old config, reproduces the new one.
    assert draft.apply(cfg, patch)["discovery"] == section
