"""Builder turn state — the `state` half of an AG-UI RunAgentInput.

`state = {draftConfig, acceptance}` (PRD §5/§6). The gateway persists this on
its skill_builder_sessions row and replays it verbatim at the start of every
turn, so this module only models the shape — it never owns durability.

  * `draftConfig` is the skill config document the agent builds incrementally
    via STATE_DELTAs. It conforms to the gateway-owned
    skill-builder-config.schema.json (stubbed here until the gateway publishes
    it — see `app.skill_builder.stubs`). Kept as a free-form dict so a schema
    revision never requires a model change; `app.skill_builder.validator`
    enforces the shape against the (swappable) JSON Schema.
  * `acceptance` is the per-phase accepted-flags map the gateway maintains from
    the operator's Accept / Request-changes actions and replays each turn. The
    agent reads it to know what's settled vs still open.

Wire casing: the AG-UI envelope keys are camelCase (`draftConfig`), so the
field carries an alias and the model is populate-by-name. The exact AG-UI
shapes are pinned in `app.skill_builder.protocol.agui` — the one place that
owns protocol handling — and are provisional pending the gateway's contract #2
confirmation.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Phase iteration order (PRD §7.2). The agent proposes ONE section at a time in
# this order; `acceptance` carries one flag per phase. Kept here (not in the
# milestone-6 phases module) because both the draft skeleton and the acceptance
# map key off it.
PHASES: tuple[str, ...] = (
    "geography",
    "discovery",
    "validation",
    "contacts",
    "scoring",
)


class BuilderState(BaseModel):
    """The `state` object inside a RunAgentInput.

    `extra="ignore"` (not "forbid") on purpose: the gateway owns this envelope
    and may add fields ahead of us knowing about them; we must not reject a
    turn because the gateway grew the state shape. Unknown keys are dropped on
    parse and simply not round-tripped.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    draft_config: dict[str, Any] = Field(default_factory=dict, alias="draftConfig")
    acceptance: dict[str, bool] = Field(default_factory=dict)

    def is_phase_accepted(self, phase: str) -> bool:
        """True only when the operator has explicitly accepted `phase`."""
        return self.acceptance.get(phase, False) is True

    def next_open_phase(self) -> str | None:
        """First phase in canonical order not yet accepted, or None if all are.

        Drives the "propose one section at a time" loop (PRD §7.2): the agent
        works the next unsettled phase rather than re-proposing accepted ones.
        """
        for phase in PHASES:
            if not self.is_phase_accepted(phase):
                return phase
        return None

    def all_phases_accepted(self) -> bool:
        """True once every canonical phase is accepted — the gate for a test
        run (PRD §7.3). Phases the gateway hasn't sent a flag for count as
        not-accepted, so a partial acceptance map never reads as complete."""
        return all(self.is_phase_accepted(p) for p in PHASES)
