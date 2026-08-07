"""Model seam — decision type, fake model, wire parsing (PRD §6/§7.2)."""

from app.skill_builder import contracts
from app.skill_builder.context import CustomerContext
from app.skill_builder.model import (
    _DECISION_WIRE_SCHEMA,
    ACTIONS,
    FakeChatModel,
    ModelDecision,
    _parse_wire_decision,
)
from app.skill_builder.prompt import compose


def _prompt():
    return compose(
        customer_context=CustomerContext({"organization_name": "ACME"}),
        task="Propose the geography section.",
        tools=contracts.tool_schemas(),
        context_field_keys=contracts.context_field_keys(),
        config_positions=contracts.config_positions(),
        runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema(),
    )


def test_fake_model_default_proposes_open_phase_section():
    d = FakeChatModel().decide(
        prompt=_prompt(), messages=[], draft_config={}, open_phase="geography"
    )
    assert d.action == "propose_section"
    assert d.phase == "geography"
    assert d.section == {}


def test_fake_model_returns_scripted_decision():
    scripted = ModelDecision(action="await_human", message="thinking")
    d = FakeChatModel(scripted).decide(
        prompt=_prompt(), messages=[], draft_config={}, open_phase="geography"
    )
    assert d is scripted


def test_wire_schema_action_enum_matches_actions_constant():
    assert set(_DECISION_WIRE_SCHEMA["properties"]["action"]["enum"]) == set(ACTIONS)
    assert _DECISION_WIRE_SCHEMA["additionalProperties"] is False


def test_parse_wire_decision_decodes_section_json():
    text = (
        '{"action": "propose_section", "message": "here", "phase": "discovery", '
        '"section_json": "{\\"rules\\": [\\"anything\\"]}"}'
    )
    d = _parse_wire_decision(text)
    assert d.action == "propose_section"
    assert d.section == {"rules": ["anything"]}


def test_parse_wire_decision_without_section_json():
    d = _parse_wire_decision('{"action": "await_human", "message": "waiting"}')
    assert d.section is None
    assert d.action == "await_human"
