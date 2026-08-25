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


#: How much of a failing property's `description` to append to a `oneOf` error.
#: Measured against the four `oneOf` sites in the schema, not guessed:
#:
#:   validation.lanes[].fields[]        description absent entirely -> nothing to add
#:   scoring.factors[].source_field     411 chars, sentences end 61 / 269 / 312
#:   scoring.factors[].keywords         724 chars, sentences end 79 / 177 / 247 / 465
#:   scoring.disqualify_rules[]...      71 chars, one sentence
#:
#: 320 is the smallest budget that keeps the SHAPE-DISTINGUISHING sentence at both
#: sites that have one. For `keywords` the shape is sentence 1 ("keyword to points,
#: highest-scoring longest match wins") so almost any budget works; for
#: `source_field` it is sentence 2 ("A list means 'try these in order'"), which is
#: the string-vs-array distinction the `oneOf` is actually about — and a 240 budget
#: cuts it at 61, leaving only the identity sentence and none of the shape. That is
#: the same mistake in miniature as the one this whole feature exists to fix.
#:
#: Deliberately NOT shared with `prompt._NOTE_BUDGET`, which is a different policy
#: with a different cost: that one is paid on the cached prefix of every turn, this
#: one on a single message at failure time. Same mechanic, separate numbers, and
#: importing prompt rendering into the validator would be the wrong direction.
_ONEOF_DESCRIPTION_BUDGET = 320


def _shape_hint(err: Any) -> str:
    """The failing property's own `description`, for a `oneOf` error only.

    jsonschema's `oneOf` message is "… is not valid under any of the given
    schemas" — the least informative thing it emits, because it discards which
    branch was closest and why. The answer is usually sitting on the property that
    failed: `err.schema` for a `oneOf` is the property subschema, so its
    `description` states the accepted shapes.

    Why this exists: on 2026-08-25 the builder authored
    `keywords: ['hiring surge', ...]` — a bare list where all three accepted shapes
    carry points — and could not self-correct from the bare message. The shapes had
    to be taught up front in the prompt instead, which cost two re-pins and a
    deploy each because they had to survive a 400-char prompt budget. Delivering
    them at failure time removes that constraint: the next unstated shape needs no
    deploy at all.

    Returns "" when there is nothing useful to add, which is a real case:
    `validation.lanes[].fields[]` is a `oneOf` with no description.

    (`err.context` holds the per-branch sub-errors if a future need wants to say
    which branch came closest; the description alone is what carries the shapes.)
    """
    if err.validator != "oneOf" or not isinstance(err.schema, dict):
        return ""
    raw = err.schema.get("description")
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    if not text:
        return ""
    if len(text) > _ONEOF_DESCRIPTION_BUDGET:
        cut = text.rfind(". ", 0, _ONEOF_DESCRIPTION_BUDGET)
        text = (
            text[: cut + 1]
            if cut > 0
            else text[:_ONEOF_DESCRIPTION_BUDGET].rstrip() + "…"
        )
    return text


def issues_for_schema(
    schema: dict[str, Any], instance: Any
) -> list[ValidationIssue]:
    """Validate `instance` against `schema`; issues sorted by (location, message).

    The single jsonschema→ValidationIssue mapping — config validation and
    tool-arg validation both go through here (DRY / SRP: all schema-validation
    lives in this module). That is why `_shape_hint` lands here: every `oneOf` in
    the schema gets it at once, rather than one shape at a time via the prompt.
    """
    validator = Draft202012Validator(schema)
    issues = []
    for err in validator.iter_errors(instance):
        message = err.message
        hint = _shape_hint(err)
        if hint:
            message = f"{message} Accepted shapes: {hint}"
        issues.append(
            ValidationIssue(location=_location(err.absolute_path), message=message)
        )
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
