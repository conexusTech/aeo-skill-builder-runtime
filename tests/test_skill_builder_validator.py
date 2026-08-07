"""draftConfig validation — incremental vs complete modes (PRD §6)."""

from app.skill_builder import draft, validator


def _full():
    return draft.skeleton(
        name="ACME Prospect Scanner",
        vertical="auto parts",
        lead_type="B",  # organizations.lead_type ENUM (A / B / MIXED)
        product_description="Prospect scanner for the auto parts vertical.",
        type_="customer",
    )


def test_complete_skeleton_is_valid_both_modes():
    cfg = _full()
    assert validator.is_valid(cfg, require_complete=False)
    assert validator.is_valid(cfg, require_complete=True)


def test_incremental_allows_missing_catalog_metadata():
    # Mid-build draft with no name/slug yet — fine incrementally, not complete.
    partial = {"geography": {"scope": {"context_ref": "home_markets"}}}
    assert validator.is_valid(partial, require_complete=False)
    issues = validator.validate_config(partial, require_complete=True)
    assert issues  # missing required version/type/run_parameters


def test_bad_slug_pattern_is_rejected():
    cfg = _full()
    cfg["slug"] = "Not A Slug"
    issues = validator.validate_config(cfg, require_complete=False)
    assert any(i.location == "/slug" for i in issues)


def test_unknown_top_level_key_is_rejected():
    cfg = _full()
    cfg["totally_unexpected"] = 1
    issues = validator.validate_config(cfg)
    assert issues  # additionalProperties: false at the top level


def test_wrong_type_for_section_is_rejected():
    cfg = _full()
    cfg["geography"] = "should-be-an-object"
    issues = validator.validate_config(cfg)
    assert any(i.location == "/geography" for i in issues)


def test_issues_sorted_by_location():
    cfg = _full()
    cfg["slug"] = "Bad Slug"
    cfg["execution_phases"] = "not-an-array"
    issues = validator.validate_config(cfg)
    locations = [i.location for i in issues]
    assert locations == sorted(locations)
