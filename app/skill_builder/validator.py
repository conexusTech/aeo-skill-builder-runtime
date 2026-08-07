"""Client-side draftConfig validation against the (swappable) config schema.

The gateway enforces the schema hard at three points and rejects violations
back into the conversation (PRD §6). We validate on our side too so the agent
can catch and repair problems BEFORE emitting — cheaper than a round-trip
rejection, and it keeps obviously-malformed config off the wire.

Two modes:
  * incremental (default) — structure/type checks only; `required` catalog
    metadata is NOT enforced, because a draft mid-build legitimately lacks a
    name/slug until later phases. Used on every STATE_DELTA.
  * complete (`require_complete=True`) — the full schema including `required`.
    Used at the test-run and finalize gates (PRD §7.3/§7.4), mirroring what the
    gateway will enforce.

Returns a list of issues rather than raising: an empty list means valid, and a
non-empty list is exactly what the conversation-repair path consumes (PRD §8).
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from app.skill_builder import contracts


class ValidationIssue(BaseModel):
    """One schema violation, addressed by a JSON-Pointer-ish location so the
    agent can point the operator (or its own repair) at the offending section."""

    location: str
    message: str


def _schema(require_complete: bool) -> dict[str, Any]:
    schema = contracts.config_schema()
    if require_complete:
        # Validators never mutate the schema, so sharing the cached dict is safe.
        return schema
    # Incremental build: drop the top-level `required` so a partial draft isn't
    # failed for missing catalog metadata. A shallow copy is enough — only a
    # top-level key is removed; nested schema objects are not touched.
    variant = dict(schema)
    variant.pop("required", None)
    return variant


def _location(path: Any) -> str:
    """Render a jsonschema error path (a deque of keys/indices) as /a/b/0."""
    parts = [str(p) for p in path]
    return "/" + "/".join(parts) if parts else "/"


def issues_for_schema(
    schema: dict[str, Any], instance: Any
) -> list[ValidationIssue]:
    """Validate `instance` against `schema`; issues sorted by (location, message).

    The single jsonschema→ValidationIssue mapping — config validation and
    tool-arg validation both go through here (DRY / SRP: all schema-validation
    lives in this module).
    """
    validator = Draft202012Validator(schema)
    issues = [
        ValidationIssue(location=_location(err.absolute_path), message=err.message)
        for err in validator.iter_errors(instance)
    ]
    issues.sort(key=lambda i: (i.location, i.message))
    return issues


def validate_config(
    config: dict[str, Any], *, require_complete: bool = False
) -> list[ValidationIssue]:
    """Validate `config`; return issues sorted by location (empty = valid)."""
    return issues_for_schema(_schema(require_complete), config)


def is_valid(config: dict[str, Any], *, require_complete: bool = False) -> bool:
    return not validate_config(config, require_complete=require_complete)


def validate_state_envelope(envelope: dict[str, Any]) -> list[ValidationIssue]:
    """Validate an AG-UI state envelope against contract #4.

    Separate from `validate_config` on purpose — the envelope carries the config
    rather than being one, so validating a `draftConfig` against the envelope
    schema (or vice versa) passes vacuously. `draftConfig` content is checked by
    `validate_config`; this is the two-step split the gateway also uses.

    Not called on the emit path: we construct the envelope ourselves, so a
    per-turn check could only fail on our own bug and costs a validation per
    turn. Its job is drift detection — point SKILL_BUILDER_STATE_ENVELOPE_PATH
    at the gateway's file and a conformance run fails on a bump instead of the
    wire failing later.
    """
    return issues_for_schema(contracts.state_envelope_schema(), envelope)


def validate_run_finished_result(result: dict[str, Any]) -> list[ValidationIssue]:
    """Validate a `RUN_FINISHED.result` against contract #5.

    Unlike the state envelope, this contract's root is CLOSED, so this catches a
    class of change we can introduce ourselves: `AGUIEmitter.interrupt` forwards
    any `**detail` its caller passes, and an undeclared key is now rejected by the
    gateway rather than ignored. Not on the emit path — a per-event validation
    would cost a schema run per turn to catch a mistake only a code change can
    make, so it is asserted over every terminal path in the tests instead.
    """
    return issues_for_schema(contracts.run_finished_schema(), result)
