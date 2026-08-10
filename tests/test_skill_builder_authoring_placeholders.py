"""Our port of the gateway's unfilled-authoring lint (#27 §3).

The two positive cases use the strings backend observed VERBATIM in a real
session, so this suite fails if our port stops agreeing with the lint that
actually gates finalize — which is the failure mode that matters here. A port
that disagrees is worse than no port: it produces a confident wrong verdict.
"""

from app.skill_builder.authoring_placeholders import lint_authoring_placeholders


def _locations(issues):
    return [i.location for i in issues]


def test_inline_context_ref_is_reported():
    """Observed verbatim. The dangerous one: right vocabulary, wrong shape.

    `resolve()` walks for binding OBJECTS and never inspects string contents,
    so the org's competitors are never substituted and this literal text is
    what gets searched.
    """
    config = {
        "discovery": {
            "sources": {
                "web": {
                    "queries": [
                        "alternatives to {context_ref:competitors} for businesses in {market}"
                    ]
                }
            }
        }
    }
    issues = lint_authoring_placeholders(config)

    assert len(issues) == 1
    assert _locations(issues) == ["discovery.sources.web.queries[0]"]
    assert "{context_ref:competitors}" in issues[0].message
    assert "object binding" in issues[0].message, "must say what the right shape IS"


def test_bracket_placeholder_is_reported():
    """Observed verbatim — reaches the search model as literal text."""
    config = {
        "discovery": {
            "sources": {
                "web": {
                    "queries": [
                        "best {market} companies offering [product/service category] compared"
                    ]
                }
            }
        }
    }
    issues = lint_authoring_placeholders(config)

    assert len(issues) == 1
    assert "[product/service category]" in issues[0].message


def test_inline_ref_is_reported_before_brackets_in_one_string():
    """Mirrors the gateway's ordering: one string can carry both, and the
    inline ref is the more misleading, so it leads the report. Ordering is
    copied rather than re-derived — divergence is the risk being managed."""
    config = {"discovery": {"q": "[fill me] and {context_ref:competitors}"}}
    issues = lint_authoring_placeholders(config)

    assert len(issues) == 2
    assert "inline context reference" in issues[0].message
    assert "unfilled template placeholder" in issues[1].message


def test_walks_every_string_not_just_queries():
    """Both observed instances were in `queries`, but neither defect is
    specific to that position. A check scoped to where a defect first appeared
    is the kind that misses its own second instance."""
    config = {"scoring": {"notes": "weight by [industry_classification]"}}
    assert _locations(lint_authoring_placeholders(config)) == ["scoring.notes"]


def test_a_correct_binding_object_is_not_flagged():
    """The whole point: an OBJECT binding is the correct shape and must pass.
    Flagging it would make the lint fire on exactly what it is asking for."""
    config = {"discovery": {"icp_attributes": {"context_ref": "competitors"}}}
    assert lint_authoring_placeholders(config) == []


def test_curly_market_token_is_not_flagged():
    """`{market}` is a legitimate, supported token (a STRONG DEFAULT, not a
    defect) — `query_expansion.py` deliberately passes placeholder-free
    templates through as well. Flagging it would break authoring."""
    config = {"discovery": {"q": "best {market} HVAC contractors"}}
    assert lint_authoring_placeholders(config) == []


def test_empty_and_tiny_brackets_are_not_flagged():
    """Mirrors the gateway's {2,60} bound. `[]` and `[x]` are too short to be
    a placeholder, and an over-eager match would fire on ordinary prose."""
    config = {"a": "array[] and index[i] are not placeholders"}
    assert lint_authoring_placeholders(config) == []


# --- Conformance: cases ported directly from the gateway's own spec ----------
#
# These are THEIR test cases, not ours. Agreement with the lint that gates
# finalize is the property under test, and reasoning that our regexes "look the
# same" is exactly the kind of check that passes for the wrong reason. If they
# change a rule, one of these should go red.


def _config_with(queries):
    return {"discovery": {"sources": {"web": {"queries": queries}}}}


def test_conformance_binding_with_documented_default_is_clean():
    """Permitted by design — the schema allows `default` only adjacent to a
    context_ref, and R12 treats it as a valid binding. Our walk descends into
    the default, so this pins that descending does not create a false positive."""
    assert lint_authoring_placeholders({
        "version": "1.0",
        "geography": {
            "home_markets": {"context_ref": "home_markets", "default": ["Austin, TX"]}
        },
    }) == []


def test_conformance_market_token_and_placeholder_free_template_are_clean():
    """A placeholder-free template is deliberate for a national registry, so
    this lint must not develop an opinion about `{market}` at all."""
    assert lint_authoring_placeholders(_config_with([
        "fleet maintenance shops near {market}",
        "national directory of logistics carriers",
    ])) == []


def test_conformance_json_ish_prose_is_not_a_placeholder():
    """A lone `[]` or a numeric index is not an unfilled placeholder, and
    flagging it would train people to ignore this lint."""
    assert lint_authoring_placeholders(
        _config_with(["results [] empty", "item [1]"])
    ) == []


def test_conformance_every_placeholder_in_one_string_is_reported():
    """One issue per string, naming ALL of its placeholders — not one issue
    each, and not only the first."""
    issues = lint_authoring_placeholders(
        _config_with(["[category] providers in [region] area"])
    )
    assert len(issues) == 1
    assert "[category]" in issues[0].message
    assert "[region]" in issues[0].message


def test_conformance_empty_config_does_not_throw():
    assert lint_authoring_placeholders({}) == []
