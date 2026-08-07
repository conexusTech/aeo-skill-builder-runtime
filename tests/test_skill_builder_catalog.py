"""Library-first catalog match (PRD §7.1 / §11 / R13)."""

from app.skill_builder import catalog


def _skill(**kw):
    base = {
        "name": "Auto Parts Scanner",
        "slug": "auto-parts-prospect-scanner",
        "vertical": "auto parts",
        # R13's three dimensions all read one vocabulary (ruled 2026-08-03):
        # `lead_type` is organizations.lead_type (A/B/MIXED) and `skill_type` is
        # skills.type (customer|project) — NOT the `prospect-scanner` runtime slug.
        "lead_type": "B",
        "skill_type": "customer",
        "status": "active",
    }
    base.update(kw)
    return base


def test_matches_active_skill_on_all_three_keys():
    # Mixed case on both dimensions — matching is case-insensitive.
    hits = catalog.match([_skill()], vertical="Auto Parts", lead_type="b")
    assert len(hits) == 1
    assert hits[0].slug == "auto-parts-prospect-scanner"


def test_inactive_skills_are_excluded():
    hits = catalog.match(
        [_skill(status="draft")], vertical="auto parts", lead_type="B"
    )
    assert hits == []


def test_vertical_or_lead_type_mismatch_is_no_match():
    assert catalog.match([_skill()], vertical="hvac", lead_type="B") == []
    assert catalog.match([_skill()], vertical="auto parts", lead_type="homeowners") == []


def test_skill_type_must_match():
    assert catalog.match(
        [_skill(skill_type="sov")], vertical="auto parts", lead_type="B"
    ) == []


def test_missing_customer_keys_yield_no_match():
    assert catalog.match([_skill()], vertical=None, lead_type="B") == []
    assert catalog.match([_skill()], vertical="auto parts", lead_type=None) == []


def test_malformed_entries_are_skipped_not_fatal():
    hits = catalog.match(
        [_skill(), "garbage", 42, {"no": "required fields"}],
        vertical="auto parts",
        lead_type="B",
    )
    assert len(hits) == 1


def test_best_match_returns_first_by_slug_or_none():
    two = [_skill(slug="b-prospect-scanner"), _skill(slug="a-prospect-scanner")]
    best = catalog.best_match(two, vertical="auto parts", lead_type="B")
    assert best.slug == "a-prospect-scanner"
    assert catalog.best_match([], vertical="auto parts", lead_type="B") is None


# --- free-text `vertical` (gateway mig 094, 2026-08-04) ---------------------


def _entry(**kw):
    base = {
        "name": "Auto Parts Scanner", "slug": "auto-parts-prospect-scanner",
        "vertical": "auto parts", "lead_type": "B", "skill_type": "customer",
        "status": "active",
    }
    base.update(kw)
    return base


def test_whitespace_and_case_variants_of_a_free_text_vertical_still_match():
    """The gateway shipped `skills.vertical` as free-text VARCHAR, not the curated
    enum Q8 still owns — so casing and stray whitespace are now reachable in real
    data, and treating them as different verticals is a pure defect."""
    for variant in ("auto parts", "Auto Parts", "  auto parts  ", "Auto  Parts",
                    "AUTO\tPARTS"):
        hit = catalog.best_match(
            [_entry(vertical=variant)], vertical="auto parts", lead_type="B"
        )
        assert hit is not None, variant


def test_punctuation_and_synonyms_deliberately_do_not_match():
    """The boundary, asserted so nobody "improves" it without owning the decision.

    A MISS falls back to building new — safe and visible. A FALSE match proposes
    connecting the WRONG skill to a customer, and R13's premise is that a matched
    skill runs for a new org without a rebuild. So under-matching is the correct
    bias while the vocabulary is uncontrolled; the fix is Q8's curated list, not a
    synonym table invented here.
    """
    for near_miss in ("auto-parts", "autoparts", "automotive parts", "auto part"):
        hit = catalog.best_match(
            [_entry(vertical=near_miss)], vertical="auto parts", lead_type="B"
        )
        assert hit is None, near_miss


def test_tags_are_ignored_rather_than_matched_on():
    """`skills.tags` shipped in mig 094 but is not one of R13's three dimensions
    (vertical + lead type + skill type). Ignored via extra="ignore" rather than
    quietly becoming a fourth filter."""
    hit = catalog.best_match(
        [_entry(tags=["fleet", "b2b"])], vertical="auto parts", lead_type="B"
    )
    assert hit is not None
    assert not hasattr(hit, "tags")
