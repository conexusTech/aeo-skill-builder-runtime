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


def test_bedrock_client_dependencies_are_installed():
    """The Bedrock client's import chain must resolve in the deployed image.

    🔴 This is a REGRESSION TEST for a defect that reached production. The first
    model-backed turn against the live runtime died with
    `ModuleNotFoundError: No module named 'botocore'`: requirements.txt declared
    plain `anthropic`, not `anthropic[bedrock]`, so boto3/botocore — which
    AnthropicBedrockMantle needs for SigV4 signing — were absent.

    Nothing caught it. Every other test injects `FakeChatModel`, so the real
    client's import chain was never executed; and the dependency is invisible to
    import-grepping because botocore appears in no import statement here. It is a
    transitive runtime need of the client, not of our code.

    Constructing the client makes NO network call, so this is a pure import-chain
    check and stays fast and offline. It deliberately does not assert on
    credentials or region: a wrong model id or a missing marketplace subscription
    must surface as an in-stream RUN_ERROR at turn time, not as a test failure
    here — that distinction is the whole reason `/ping` does not resolve the model.
    """
    from anthropic import AnthropicBedrockMantle  # noqa: F401

    import boto3  # noqa: F401
    import botocore  # noqa: F401
