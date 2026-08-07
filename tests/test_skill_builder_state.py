"""BuilderState — acceptance map + phase-order helpers (app.skill_builder.state)."""

from app.skill_builder.state import PHASES, BuilderState


def test_phase_order_is_the_prd_sequence():
    assert PHASES == ("geography", "discovery", "validation", "contacts", "scoring")


def test_draft_config_accepts_camelcase_alias():
    state = BuilderState.model_validate({"draftConfig": {"name": "x"}, "acceptance": {}})
    assert state.draft_config == {"name": "x"}


def test_unknown_state_keys_are_ignored_not_rejected():
    # Gateway owns the envelope and may grow it; we must not reject a turn.
    state = BuilderState.model_validate({"draftConfig": {}, "future_field": 1})
    assert state.draft_config == {}


def test_next_open_phase_walks_canonical_order():
    state = BuilderState(acceptance={"geography": True})
    assert state.next_open_phase() == "discovery"


def test_next_open_phase_none_when_all_accepted():
    state = BuilderState(acceptance={p: True for p in PHASES})
    assert state.next_open_phase() is None
    assert state.all_phases_accepted() is True


def test_partial_acceptance_is_not_complete():
    state = BuilderState(acceptance={"geography": True, "discovery": True})
    assert state.all_phases_accepted() is False


def test_falsey_flag_counts_as_not_accepted():
    state = BuilderState(acceptance={"geography": False})
    assert state.is_phase_accepted("geography") is False
    assert state.next_open_phase() == "geography"
