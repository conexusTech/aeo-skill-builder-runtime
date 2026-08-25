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


# --- oneOf errors carry the accepted shapes (2026-08-25) --------------------
#
# jsonschema's `oneOf` message discards which branch was closest, so the model
# could not self-correct from it and the shapes had to be taught up front in the
# prompt instead -- which cost two re-pins and a deploy each, because they had to
# survive a 400-char prompt budget. These pin the fix that removes that
# constraint: the shapes arrive at FAILURE time, where there is no prompt budget.


def _factor(**over):
    f = {"name": "growth signals", "source_field": "signals", "weight": 10}
    f.update(over)
    return {"scoring": {"factors": [f]}}


def _issue_at(cfg, location):
    return next(
        (i for i in validator.validate_config(cfg) if i.location == location), None
    )


def test_a_oneOf_failure_states_the_accepted_shapes():
    """The exact config the builder authored on 2026-08-25: a bare list of category
    strings, where all three accepted shapes carry points."""
    issue = _issue_at(
        _factor(keywords=["hiring surge", "new office", "expansion"]),
        "/scoring/factors/0/keywords",
    )
    assert issue is not None, "the bare list should still be rejected"
    assert "not valid under any of the given schemas" in issue.message
    assert "Accepted shapes:" in issue.message
    # The shape itself, which is the whole point -- without this the model is told
    # only that it was wrong.
    assert "keyword to points" in issue.message


def test_the_budget_keeps_the_SHAPE_sentence_not_just_the_first_one():
    """`source_field` is the case that fixes the budget at 320 rather than lower.

    Its description's shape-distinguishing sentence -- the string-vs-array
    distinction the `oneOf` is actually about -- ends at 269, so a 240 budget cuts
    at 61 and leaves only the identity sentence. That would be this feature
    reproducing, in miniature, the bug it exists to fix.
    """
    issue = _issue_at(_factor(source_field=42), "/scoring/factors/0/source_field")
    assert issue is not None
    assert "Accepted shapes:" in issue.message
    assert "try these in order" in issue.message, (
        "the string-vs-array sentence was truncated -- the budget is too low, and "
        "what survives is the identity sentence with none of the shape"
    )


def test_a_non_oneOf_failure_gets_no_shape_hint():
    """Ordinary type/format errors are already specific, so appending a paragraph
    to them is noise. The hint is scoped to the one message that needs it."""
    cfg = _factor()
    cfg["scoring"]["factors"][0]["weight"] = "heavy"
    issue = _issue_at(cfg, "/scoring/factors/0/weight")
    assert issue is not None
    assert "Accepted shapes:" not in issue.message


def test_a_oneOf_with_no_description_degrades_silently():
    """A real case, not hypothetical: `validation.lanes[].fields[]` is a `oneOf`
    carrying no description. It must not emit a dangling 'Accepted shapes:'."""
    cfg = {"validation": {"lanes": [{"key": "k", "objective": "o", "fields": [42]}]}}
    issues = [i for i in validator.validate_config(cfg) if "fields" in i.location]
    assert issues, "the bad field entry should still be rejected"
    for i in issues:
        assert "Accepted shapes:" not in i.message
