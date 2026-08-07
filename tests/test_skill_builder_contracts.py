"""Contract loader — bundled stubs + the single override swap point."""

import hashlib
import json
from pathlib import Path

import pytest

from app.skill_builder import contracts

#: The gateway's contract directory, as checked out beside this repo. Present on
#: a dev machine, absent in CI — the drift test skips on absence and on nothing
#: else, because a blanket skip would make this file pass while comparing zero
#: bytes.
_GATEWAY_DIR = (
    Path(__file__).resolve().parents[2]
    / "aeo-backend"
    / "src"
    / "backend"
    / "skills"
    / "config"
)

#: bundled pin → the gateway file it is a verbatim copy of.
_PINS = {
    "config_schema.json": "skill-builder-config.schema.json",
    "context_field_keys.json": "context-field-keys.json",
    "tool_schemas.json": "tool-call-schemas.json",
    "agui_state_envelope.json": "agui-state-envelope.json",
    "agui_run_finished.json": "agui-run-finished.json",
}


def test_config_schema_is_the_ratified_contract():
    # The bundled default is a pinned verbatim copy of the gateway's RATIFIED v1
    # (it was a hand-written stub until 2026-08-03). A guessed stub that
    # contradicts a ratified contract is worse than no stub: it makes
    # "schema-valid" mean valid against a fiction.
    schema = contracts.config_schema()
    assert "RATIFIED" in schema["title"]
    assert schema["required"] == ["version", "run_parameters"]
    # Closed root envelope: a camelCase or typo'd key must fail loudly rather
    # than being accepted and silently dropped.
    assert schema["additionalProperties"] is False
    # `execution_phases` is declared but NOT required — it is a runtime concept
    # the gateway strips before we see a config, so it is not ours to author.
    assert "execution_phases" in schema["properties"]
    assert "execution_phases" not in schema["required"]
    # 🔴 `type` is declared but NOT required, since 2026-08-07 (thread #21).
    #
    # It was in `required` and we have never emitted it: `draft.skeleton()` accepts
    # `type_` and its one caller (runtime.py:149) does not pass it, deliberately —
    # the field is enum-constrained and the authoring rule is "omit what isn't known,
    # never placeholder it". So STRICT validation rejected every draft we produce, at
    # BOTH gates (test-run start and finalize). It survived because R2 did not exist,
    # so no config of ours had ever reached a strict gate; backend found it by driving
    # their new pipe against our container.
    #
    # Asserted rather than left implicit because this is the SECOND time this exact
    # defect shipped on a neighbouring field — `execution_phases` above was the first,
    # same cause, corrected in thread #07.1. Two instances is a pattern, so both
    # now have a standing assertion.
    assert "type" in schema["properties"]
    assert "type" not in schema["required"]


def test_context_field_keys_reads_the_flat_key_list():
    keys = contracts.context_field_keys()
    assert isinstance(keys, frozenset)
    assert len(keys) == 13
    assert "home_markets" in keys
    assert "decision_titles" in keys
    # The canonical vocabulary is UNPREFIXED. Our pre-ratification stub used
    # `customer.`-prefixes, and an unknown key is a HARD R12 lint failure, so a
    # regression here breaks configs rather than degrading them.
    assert not any(k.startswith("customer.") for k in keys)


def test_empty_context_field_vocabulary_fails_loudly(tmp_path, monkeypatch):
    """An empty vocabulary must raise, not degrade.

    Allowed through, the prompt's bindings layer still tells the model a
    `context_ref` is mandatory and then lists no legal key — so it either invents
    one (hard R12 lint failure) or emits a bare literal (org_coupling). Every
    draft would be rejected at test-run/finalize with nothing wrong at load time.
    """
    for doc in ({"key_list": []}, {"keys": []}, {"keys": {}}, {}):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps(doc), encoding="utf-8")

        class _Settings:
            SKILL_BUILDER_CONFIG_SCHEMA_PATH = ""
            SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH = str(path)
            SKILL_BUILDER_TOOL_SCHEMAS_PATH = ""

        monkeypatch.setattr(contracts, "get_settings", lambda: _Settings())
        contracts._load_json.cache_clear()
        with pytest.raises(ValueError, match="empty vocabulary"):
            contracts.context_field_keys()
    contracts._load_json.cache_clear()


def test_context_field_keys_accepts_all_three_published_shapes(tmp_path, monkeypatch):
    """`key_list` wins; a bare list or a metadata map both still work.

    The ratified file publishes the vocabulary twice — `key_list` (flat) and
    `keys` (same set, with per-key metadata). Reading only one of them is how
    this loader broke on the first repoint.
    """
    cases = {
        "key_list.json": {"key_list": ["a", "b"], "keys": {"a": {}, "b": {}}},
        "bare_list.json": {"keys": ["a", "b"]},
        "map_only.json": {"keys": {"a": {"brief_path": "x"}, "b": {}}},
    }
    for filename, doc in cases.items():
        path = tmp_path / filename
        path.write_text(json.dumps(doc), encoding="utf-8")

        class _Settings:
            SKILL_BUILDER_CONFIG_SCHEMA_PATH = ""
            SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH = str(path)
            SKILL_BUILDER_TOOL_SCHEMAS_PATH = ""

        monkeypatch.setattr(contracts, "get_settings", lambda: _Settings())
        contracts._load_json.cache_clear()
        assert contracts.context_field_keys() == frozenset({"a", "b"}), filename
    contracts._load_json.cache_clear()


def test_state_envelope_schema_is_the_ratified_contract():
    """Contract #4 — the envelope, not the config document.

    Published on 2026-08-03 because our own emitter proved the envelope had no
    contract: we emitted config-rooted deltas against a `{draftConfig,
    acceptance}` snapshot, and nothing on the wire declared either root.
    """
    schema = contracts.state_envelope_schema()
    assert "RATIFIED" in schema["title"]
    assert schema["required"] == ["draftConfig", "acceptance"]
    # `acceptance` is a SIBLING of draftConfig, never nested inside it —
    # conversation bookkeeping must not reach `skills.config` at finalize.
    assert set(schema["properties"]) == {"draftConfig", "acceptance"}
    # Open at the root: the gateway owns this envelope and may grow it, and we
    # must not reject a turn because it did. (Contrast the CONFIG root, which is
    # deliberately closed — a casing slip there is data loss.)
    assert schema["additionalProperties"] is True


def test_pinned_contracts_match_the_gateways_files():
    """The pins must be verbatim copies, and only a diff proves it.

    "Pinned verbatim copy" is an invariant we assert in prose in four places and
    could not previously detect breaking. The config schema drifted on
    2026-08-04 (description + `$comment` only, so nothing validated
    differently) and was found by hand-hashing, not by a test.
    """
    if not _GATEWAY_DIR.is_dir():
        pytest.skip(f"gateway contracts not checked out at {_GATEWAY_DIR}")

    compared, drifted = [], []
    for pin, published in _PINS.items():
        source = _GATEWAY_DIR / published
        if not source.is_file():
            drifted.append(f"{published}: missing from the gateway directory")
            continue
        # Newline-normalised, not raw bytes: both repos run core.autocrlf=true
        # with no .gitattributes, so a re-clone can legitimately hand one of them
        # CRLF. Comparing raw bytes would report "contract drift" for a checkout
        # artefact, and a drift test that cries wolf gets muted. Content is the
        # invariant; any real edit still changes the digest.
        ours = (Path(contracts._STUB_DIR) / pin).read_bytes().replace(b"\r\n", b"\n")
        theirs = source.read_bytes().replace(b"\r\n", b"\n")
        compared.append(pin)
        if hashlib.sha256(ours).digest() != hashlib.sha256(theirs).digest():
            drifted.append(
                f"{pin} != {published} — re-pin it, and check whether the "
                "change is behavioural before assuming it is cosmetic"
            )

    # Guards the skip above: a path typo would otherwise leave this test green
    # having compared nothing at all.
    assert sorted(compared) == sorted(_PINS), f"only compared {compared}"
    assert not drifted, "\n".join(drifted)


def test_tool_schemas_expose_both_tools():
    tools = contracts.tool_schemas()
    assert set(tools) == {"request_test_run", "request_finalize"}
    assert "input_schema" in tools["request_test_run"]


def test_override_path_swaps_the_contract(tmp_path, monkeypatch):
    # Simulate the gateway publishing a real schema at an override path.
    real = tmp_path / "real_schema.json"
    real.write_text(json.dumps({"title": "REAL", "type": "object"}), encoding="utf-8")

    class _Settings:
        SKILL_BUILDER_CONFIG_SCHEMA_PATH = str(real)
        SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH = ""
        SKILL_BUILDER_TOOL_SCHEMAS_PATH = ""

    monkeypatch.setattr(contracts, "get_settings", lambda: _Settings())
    contracts._load_json.cache_clear()
    assert contracts.config_schema()["title"] == "REAL"
    contracts._load_json.cache_clear()
