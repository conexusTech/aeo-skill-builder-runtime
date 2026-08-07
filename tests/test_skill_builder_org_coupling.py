"""R12 org-coupling lint — our port of the gateway's verdict (thread #13.6).

The point of this port is that our verdict AGREES with the one that gates
finalize. So these tests are written against the published contract and the
gateway's own documented behaviour, not against our implementation's habits.
"""

import pytest

from app.skill_builder import contracts, tools
from app.skill_builder.org_coupling import lint_org_coupling
from app.skill_builder.protocol.agui import AGUIEmitter, EventType


def _locations(config):
    return [i.location for i in lint_org_coupling(config)]


def test_published_positions_are_the_nine_and_carry_the_two_undeducible_ones():
    positions = contracts.config_positions()
    assert len(positions) == 9
    mapping = {(p.section, p.key): p.context_ref for p in positions}
    # These two are the reason this mapping had to be published rather than
    # derived: the config key and the context key differ, so a model authoring
    # `contacts` would emit `{"context_ref": "titles"}` and fail the lint HARD.
    assert mapping[("contacts", "titles")] == "decision_titles"
    assert mapping[("contacts", "seniorities")] == "decision_seniorities"
    # Every position's expected ref must exist in the closed vocabulary, or our
    # error messages would suggest a key that is itself rejected.
    keys = contracts.context_field_keys()
    unknown = sorted({r for r in mapping.values() if r not in keys})
    assert not unknown, f"config_positions names refs absent from key_list: {unknown}"


def test_a_binding_at_every_published_position_is_clean():
    """Derived from the contract, not hand-listed — so adding a position to the
    published array extends this test automatically instead of silently not
    covering the new one."""
    config = {}
    for position in contracts.config_positions():
        config.setdefault(position.section, {})[position.key] = {
            "context_ref": position.context_ref
        }
    assert lint_org_coupling(config) == []


def test_a_literal_at_every_published_position_is_rejected():
    for position in contracts.config_positions():
        config = {position.section: {position.key: ["Phoenix"]}}
        issues = lint_org_coupling(config)
        assert [i.location for i in issues] == [f"{position.section}.{position.key}"], (
            f"{position.section}.{position.key} not flagged"
        )
        # The message must name the ref to bind, or the repair loop has to guess
        # exactly the thing that was undeducible in the first place.
        assert position.context_ref in issues[0].message


def test_the_default_sibling_is_the_one_legal_place_for_a_literal():
    clean = {"geography": {"excluded_markets": {"context_ref": "excluded_markets",
                                               "default": ["Reno"]}}}
    assert lint_org_coupling(clean) == []


def test_a_prefixed_or_dotted_ref_is_rejected_not_normalised():
    """The vocabulary is unprefixed, and an unknown ref is a HARD failure — it
    resolves to nothing at scan time, so targeting silently narrows or widens."""
    for bad in ("customer.home_markets", "context.geography.home_markets",
                "{{context.geography.home_markets}}", "titles"):
        issues = lint_org_coupling({"geography": {"home_markets": {"context_ref": bad}}})
        assert [i.location for i in issues] == ["geography.home_markets.context_ref"], bad


def test_absent_and_empty_values_are_not_this_lints_business():
    """R12 governs how a value is expressed, never whether it had to be authored
    — requiredness is the schema's job. Mirrors the gateway's `isEmpty`."""
    for value in (None, [], "", "   "):
        assert lint_org_coupling({"geography": {"home_markets": value}}) == []
    # Section absent entirely, and section present but not an object.
    assert lint_org_coupling({}) == []
    assert lint_org_coupling({"geography": ["not", "an", "object"]}) == []
    assert lint_org_coupling({"geography": None}) == []


def test_an_empty_dict_is_not_treated_as_empty():
    """Deliberate parity with the gateway's `isEmpty`, which returns false for an
    object. `{}` at an org-specific position is a literal that binds nothing, and
    treating it as absent would let it through both gates."""
    issues = lint_org_coupling({"geography": {"home_markets": {}}})
    assert [i.location for i in issues] == ["geography.home_markets"]


def test_stray_refs_are_validated_anywhere_in_the_document():
    """The model may bind fields nobody enumerated; an unresolvable ref is a
    defect wherever it appears, including inside arrays."""
    config = {
        "scoring": {"weights": [{"context_ref": "not_a_key"}]},
        "discovery": {"nested": {"deep": {"context_ref": "also_not_a_key"}}},
    }
    assert sorted(_locations(config)) == [
        "discovery.nested.deep.context_ref",
        "scoring.weights.0.context_ref",
    ]


def test_a_binding_default_is_not_descended_into():
    """A `default` legitimately holds the org's literal values, and one of them
    could itself be a dict with a `context_ref`-shaped key. Descending would
    report a violation inside the one place a literal is allowed."""
    config = {
        "geography": {
            "home_markets": {
                "context_ref": "home_markets",
                "default": {"context_ref": "this_is_data_not_a_binding"},
            }
        }
    }
    assert lint_org_coupling(config) == []


def test_an_enumerated_position_is_reported_once_not_twice():
    """Both passes visit the same node; the recursive one must defer."""
    config = {"geography": {"home_markets": {"context_ref": "bogus"}}}
    assert _locations(config) == ["geography.home_markets.context_ref"]


def test_locations_are_dotted_to_match_the_gateways_coupling_verdict():
    """Not a style choice: an operator comparing our pre-emit message against a
    gateway rejection must see the same string. (Their SCHEMA errors use a
    different format, so their own issues[] carries both.)"""
    issues = lint_org_coupling({"contacts": {"titles": ["Owner"]}})
    assert issues[0].location == "contacts.titles"
    assert "/" not in issues[0].location


def test_missing_config_positions_block_raises_rather_than_degrading(tmp_path, monkeypatch):
    """Without the block the lint would only catch stray refs — so a literal at
    `geography.home_markets` would pass every check we make and be rejected at
    the gateway's finalize gate, which is the failure this port exists to move
    earlier. Silence is the one unacceptable outcome."""
    import json

    path = tmp_path / "no_positions.json"
    path.write_text(json.dumps({"key_list": ["home_markets"]}), encoding="utf-8")

    class _Settings:
        SKILL_BUILDER_CONFIG_SCHEMA_PATH = ""
        SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH = str(path)
        SKILL_BUILDER_TOOL_SCHEMAS_PATH = ""
        SKILL_BUILDER_STATE_ENVELOPE_PATH = ""

    monkeypatch.setattr(contracts, "get_settings", lambda: _Settings())
    contracts._load_json.cache_clear()
    with pytest.raises(ValueError, match="config_positions"):
        lint_org_coupling({"geography": {"home_markets": ["Phoenix"]}})
    contracts._load_json.cache_clear()


def test_tool_gate_blocks_a_coupled_config_before_emitting():
    """The payoff: no tool call escapes with a coupling violation, so the model
    repairs in-conversation instead of being rejected after the call."""
    complete = {
        "version": "1.0",
        "type": "customer",
        "run_parameters": {},
        "geography": {"home_markets": ["Phoenix", "Tempe"]},  # literal — violation
    }
    emitter = AGUIEmitter(thread_id="t")
    outcome = tools.request_finalize(emitter, complete)
    assert outcome.requested is False
    assert EventType.TOOL_CALL_START not in [e.type for e in emitter.events]
    assert any(i.location == "geography.home_markets" for i in outcome.issues)
    # And the operator-visible text names the position and the fix.
    text = " ".join(
        e["delta"] for e in emitter.wire_events() if e["type"] == EventType.TEXT_MESSAGE_CONTENT
    )
    assert "geography.home_markets" in text
    assert "home_markets" in text


def test_section_gate_refuses_to_emit_a_coupled_section():
    """The earliest gate, and the one that matters most for repair quality.

    At the tool gate the model has moved on; here it is still holding the section
    that caused the violation, so "bind `contacts.titles` to `decision_titles`"
    is actionable in one turn. Without this the literal is written into
    `draftConfig`, persisted by R2, and only rejected at finalize.
    """
    from app.skill_builder.model import FakeChatModel, ModelDecision
    from app.skill_builder.runtime import handle_turn

    payload = {
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "proposal"},
            {"role": "user", "content": "ok"},
        ],
        "state": {"draftConfig": {}, "acceptance": {"geography": True,
                                                   "discovery": True,
                                                   "validation": True}},
        "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
    }
    model = FakeChatModel(ModelDecision(
        action="propose_section", message="Proposed contacts.", phase="contacts",
        # The natural mistake the published mapping exists to prevent.
        section={"titles": ["Owner", "General Manager"]},
    ))
    res = handle_turn(payload, model=model)
    types = [e.type for e in res.emitter.events]
    assert EventType.STATE_DELTA not in types, "a coupled section must not reach the wire"
    text = " ".join(
        e["delta"] for e in res.emitter.wire_events()
        if e["type"] == EventType.TEXT_MESSAGE_CONTENT
    )
    assert "contacts.titles" in text
    assert "decision_titles" in text


def test_mirrors_the_gateways_spec_fixture():
    """Cross-checked against `org-coupling.lint.spec.ts`'s own shape: a document
    with one literal position and one bad stray ref yields exactly two issues."""
    config = {
        "geography": {"home_markets": ["Phoenix"]},
        "contacts": {"titles": {"context_ref": "decision_titles"}},
        "scoring": {"emphasis": {"context_ref": "nope"}},
    }
    assert sorted(_locations(config)) == [
        "geography.home_markets",
        "scoring.emphasis.context_ref",
    ]


# --- the third rule kind: runtime-populated positions (thread #17) -----------


def _sourced(seed_firms):
    return {"discovery": {"sources": {"firms": {
        "name_field": "name", "fields": ["name"],
        "queries": ["auto parts distributors in {market}"],
        "seed_firms": seed_firms,
    }}}}


def test_the_published_rule_is_the_seed_firms_one():
    positions = contracts.runtime_populated_positions()
    assert len(positions) == 1
    p = positions[0]
    assert (p.section, p.collection, p.leaf) == ("discovery", "sources", "seed_firms")
    assert p.populated_from == "lookalike_sources"
    assert p.location == "discovery.sources.*.seed_firms"


def test_an_authored_seed_firms_literal_is_rejected():
    """The half that actually closes the hole.

    A fan-out in the runtime creates a correct path but leaves the incorrect one
    passing — and a literal list is what a model reaches for, since it is the
    obvious way to express "firms we already know". Authored here it travels with
    the skill, and the next org to connect it searches using the first org's
    customers.
    """
    issues = lint_org_coupling(_sourced(["Acme Parts", "Bob's Auto"]))
    assert [i.location for i in issues] == ["discovery.sources.firms.seed_firms"]
    assert "lookalike_sources" in issues[0].message


def test_a_binding_at_a_runtime_populated_position_is_also_rejected():
    """"Not authored at all" means not a binding either — the runtime already
    supplies the value, so even a correct `context_ref` is a second, competing
    source for one field."""
    issues = lint_org_coupling(_sourced({"context_ref": "lookalike_sources"}))
    assert any(i.location == "discovery.sources.firms.seed_firms" for i in issues)


def test_an_absent_or_empty_seed_firms_is_clean():
    for value in (None, [], ""):
        assert lint_org_coupling(_sourced(value)) == []
    clean = {"discovery": {"sources": {"firms": {
        "name_field": "name", "fields": ["name"], "queries": ["x in {market}"],
    }}}}
    assert lint_org_coupling(clean) == []


def test_every_source_is_checked_not_just_the_first():
    config = {"discovery": {"sources": {
        "a": {"queries": ["x in {market}"]},
        "b": {"seed_firms": ["Acme"]},
        "c": {"seed_firms": ["Beta"]},
    }}}
    assert sorted(i.location for i in lint_org_coupling(config)) == [
        "discovery.sources.b.seed_firms",
        "discovery.sources.c.seed_firms",
    ]


def test_an_unsupported_path_pattern_raises_rather_than_silently_not_matching(
    tmp_path, monkeypatch
):
    """Their own reason for the flat grammar: a pattern this code cannot match
    would make the lint pass by accident, which is the failure mode the rule was
    added to leave behind."""
    import json

    path = tmp_path / "bad_pattern.json"
    path.write_text(json.dumps({
        "key_list": ["lookalike_sources"],
        "config_positions": [{"section": "discovery", "key": "lookalike_sources",
                              "context_ref": "lookalike_sources"}],
        "runtime_populated_positions": [
            {"section": "discovery", "path": "sources.*.deep.*.leaf",
             "populated_from": "lookalike_sources"}
        ],
    }), encoding="utf-8")

    class _Settings:
        SKILL_BUILDER_CONFIG_SCHEMA_PATH = ""
        SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH = str(path)
        SKILL_BUILDER_TOOL_SCHEMAS_PATH = ""
        SKILL_BUILDER_STATE_ENVELOPE_PATH = ""
        SKILL_BUILDER_RUN_FINISHED_PATH = ""

    monkeypatch.setattr(contracts, "get_settings", lambda: _Settings())
    contracts._load_json.cache_clear()
    with pytest.raises(ValueError, match="not the one supported pattern"):
        contracts.runtime_populated_positions()
    contracts._load_json.cache_clear()


def test_the_prompt_teaches_the_shapes_and_the_one_hard_rule():
    """`seed_firms` is the only hard prohibition; `{market}` is a strong default.

    Backend corrected both on 2026-08-05: they had told us both were lint-enforced,
    but only `seed_firms` is — and the `{market}` rule as originally stated was
    *wrong*, because `query_expansion.py` passes a placeholder-free template
    through unchanged on purpose (a national registry is legitimately
    geography-free). Teaching it as MUST would have made the model unable to
    author that case. This asserts the corrected framing survives, since the wrong
    version is the intuitive one to drift back to.
    """
    from app.skill_builder.context import CustomerContext
    from app.skill_builder.prompt import compose

    rendered = compose(
        customer_context=CustomerContext({"organization_name": "ACME"}),
        task="t", tools={},
        context_field_keys=contracts.context_field_keys(),
        config_positions=contracts.config_positions(),
        runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema(),
    ).render()

    # The shapes, derived from the schema rather than hand-maintained.
    for shape_key in ("name_field", "in_market_signals", "seniorities",
                      "use_zip_discovery", "geo_strictness", "sources"):
        assert shape_key in rendered, shape_key

    # The hard prohibition, from two independent places (lint vocabulary + schema).
    assert "discovery.sources.*.seed_firms" in rendered
    assert "NEVER author" in rendered

    # `{market}` is taught WITH its exception. The exception is the load-bearing
    # half: without it this reads as an absolute again.
    assert "{market}" in rendered
    assert "national registry" in rendered
