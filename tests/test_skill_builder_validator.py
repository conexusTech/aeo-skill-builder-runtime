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


def test_a_shape_error_with_no_description_anywhere_degrades_silently():
    """A dangling "Accepted shapes:" with nothing after it would be worse than no
    hint at all, so the empty case is asserted directly on the helper.

    Asserted synthetically rather than on a real path, and that is a change from
    the first version of this test: it used `validation.lanes[].fields[]`, which was
    a `oneOf` with no description ON THE ONEOF NODE. Resolving the description by
    walking the PATH now finds the `fields` array's description one level up, so
    that site legitimately gets a hint and no longer demonstrates the empty case.
    The guard still has to exist, so it is pinned where it cannot drift.
    """
    from jsonschema import Draft202012Validator

    from app.skill_builder.validator import _shape_hint

    schema = {
        "type": "object",
        "properties": {"row": {"type": "object", "required": ["a"]}},
    }
    errs = [
        e
        for e in Draft202012Validator(schema).iter_errors({"row": {}})
        if e.validator == "required"
    ]
    assert errs, "expected a required-property error to test against"
    assert _shape_hint(schema, errs[0]) == ""


def test_a_required_error_states_the_shape_via_the_OWNING_description():
    """The 2026-08-25 fourth-round bug: the builder authored tier rows as
    `{min, max, points}` instead of `{threshold, points}`.

    `tiers.description` says in so many words to use `tiers` INSTEAD of min/max,
    and the model never saw it: the prompt's shapes layer expands one level and
    stops above `tiers`, and `tiers` has no `oneOf` so the first version of this
    feature did not reach it either. The description lives on the `tiers` ARRAY
    while the error points at a ROW, which is why the lookup walks the path rather
    than reading `err.schema`.
    """
    cfg = _factor(tiers=[{"min": 0, "max": 100, "points": 5}])
    issues = [i for i in validator.validate_config(cfg) if "tiers" in i.location]
    assert issues, "a {min,max,points} tier row should be rejected"

    # Pinned on the `required` issue SPECIFICALLY, not on the joined messages. The
    # first version of this asserted against all tiers messages concatenated, and a
    # mutation dropping `required` from the shape validators still passed -- the
    # `additionalProperties` error alone satisfied it. Same "assert the property,
    # not the symptom" trap this file already documents, and the harness caught it.
    required_issue = next(
        (i for i in issues if "is a required property" in i.message), None
    )
    assert required_issue is not None, "expected a required-property error"
    assert "Accepted shapes:" in required_issue.message
    # The correction itself, not merely the identity sentence.
    assert "instead of min/max" in required_issue.message, (
        "the min/max disambiguation was truncated -- that is the one sentence this "
        "extension exists to deliver"
    )


def test_the_budget_keeps_the_tiers_min_max_sentence():
    """`tiers` fixes the budget at 340 and nothing lower.

    Its sentences end at 79 / 331 / 368 / 516 / 599. The min/max correction is
    sentence 2, ending at 331, so a 320 budget cuts at 79 and delivers the
    description WITHOUT the correction -- this feature failing the same way twice.
    """
    from app.skill_builder.validator import _SHAPE_DESCRIPTION_BUDGET

    assert _SHAPE_DESCRIPTION_BUDGET >= 331, (
        "below 331 the tiers description arrives without its min/max sentence, "
        "which is the only reason this reaches tiers at all"
    )


def test_additional_properties_also_gets_the_shape():
    """`additionalProperties: false` names the offending key and never the shape.
    Same gap as `oneOf`, same fix."""
    cfg = _factor(tiers=[{"threshold": 10, "points": 5, "nonsense": 1}])
    issues = [
        i
        for i in validator.validate_config(cfg)
        if "tiers" in i.location and "Additional properties" in i.message
    ]
    assert issues, "an unknown tier key should be rejected"
    assert "Accepted shapes:" in issues[0].message


def test_a_value_error_still_gets_no_shape_hint():
    """Type/enum/format errors are already specific about the one value at fault,
    so a paragraph appended to them is noise. The hint is scoped to SHAPE errors."""
    cfg = _factor()
    cfg["scoring"]["factors"][0]["weight"] = "heavy"
    issue = _issue_at(cfg, "/scoring/factors/0/weight")
    assert issue is not None
    assert "Accepted shapes:" not in issue.message
