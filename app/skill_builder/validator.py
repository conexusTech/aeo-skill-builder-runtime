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

from jsonschema import validators
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


#: How much of the owning property's `description` to append to a SHAPE error.
#: Measured against every site a shape error can reach, not guessed. The number is
#: fixed by `tiers`, and the reason is the whole point of this feature:
#:
#:   scoring.factors[].tiers          599 chars, sentences end  79 / 331 / 368 / 516
#:   scoring.factors[].keywords       724 chars,                79 / 177 / 247 / 465
#:   scoring.factors[].source_field   411 chars,                61 / 269 / 312
#:   scoring.priority_bands           408 chars,                20 / 221
#:   scoring.disqualify_rules         692 chars,                139 / 341 / 424
#:   validation.lanes[].fields        271 chars,                30 / 204  (all of it)
#:
#: `tiers` sentence 2 ends at **331**: *"Use this instead of min/max whenever the
#: criterion is a curve rather than a cutoff"*. That is the sentence that prevents
#: the exact bug this extension exists for — the builder authoring tier rows as
#: `{min, max, points}` — so a 320 budget would deliver the description WITHOUT the
#: correction, which is this feature failing in the same way twice. 340 is the
#: smallest number that keeps it. A test pins that boundary.
#:
#: ⚠️ Deliberately cut: `tiers` sentence 5, *"Authoring this switches the factor to
#: graded mode and `min`/`max` are then ignored"* (ends 599). It reinforces sentence
#: 2 rather than adding a shape, and reaching it would nearly double every message
#: and drag in `disqualify_rules` sentences 2-4, which are semantics rather than
#: shape. If it turns out to be needed, the cheap fix is backend reordering `tiers`
#: to put it beside sentence 2 — the same "owner reorders, budget does not move"
#: settlement as `prompt._NOTE_BUDGET`.
#:
#: Not shared with `_NOTE_BUDGET`: same mechanic, different policy and cost — that
#: one is paid on the cached prefix of every turn, this one on one message at
#: failure time — and validator→prompt would be the wrong dependency direction.
_SHAPE_DESCRIPTION_BUDGET = 340

#: Errors about the SHAPE of an object, where "what is the accepted shape?" is the
#: question the message fails to answer. Value errors (`type`, `enum`, `format`,
#: `pattern`, bounds) are excluded on purpose: they are already specific about the
#: one value at fault, so appending a paragraph to them is noise.
#:
#: `oneOf` came first (2026-08-25) because its message is the least informative
#: jsonschema emits. `required` and `additionalProperties` were added the same day
#: after a fourth round of the same failure: the builder authored tier rows as
#: `{min, max, points}`, and `tiers.description` — which says in so many words to
#: use `tiers` INSTEAD of min/max — never reached it, because the prompt's shapes
#: layer expands one level and stops above `tiers`.
_SHAPE_VALIDATORS = frozenset({"oneOf", "required", "additionalProperties"})


def _resolve_ref(schema: dict[str, Any], node: Any) -> Any:
    """Follow a local `$ref` one hop. The schema carries 22 of them via `$defs`."""
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target: Any = schema
    for part in ref[2:].split("/"):
        if not isinstance(target, dict):
            return node
        target = target.get(part)
    return target if isinstance(target, dict) else node


def _owning_description(schema: dict[str, Any], path: Any) -> str:
    """The DEEPEST description at or above `path`.

    Walking the path rather than reading `err.schema` is what makes this work for
    `required` / `additionalProperties`: those errors point at the object that is
    malformed (a tier ROW), and the description that explains the shape lives on
    its parent (the `tiers` array). `err.schema` would be the row, which has none.
    """
    node: Any = schema
    found = ""
    for segment in path:
        node = _resolve_ref(schema, node)
        if not isinstance(node, dict):
            return found
        if isinstance(segment, int):
            node = node.get("items")
        else:
            node = (node.get("properties") or {}).get(segment)
        node = _resolve_ref(schema, node)
        if isinstance(node, dict):
            candidate = node.get("description")
            if isinstance(candidate, str) and candidate.strip():
                found = candidate
    return found


def _shape_hint(schema: dict[str, Any], err: Any) -> str:
    """The owning property's `description` for a shape error, else "".

    Why this exists: jsonschema tells the model what is wrong and not what is
    right. `oneOf` is the worst — "is not valid under any of the given schemas"
    discards which branch was closest — but `required` and `additionalProperties`
    have the same gap: they name the offending key and never state the shape.

    On 2026-08-25 that cost four rounds of prompt-text edits, each a re-pin and a
    deploy, because every accepted shape had to survive a 400-char prompt budget
    up front. Delivering the description at failure time removes that constraint:
    there is no budget at the point of failure, and the schema already says it.

    Returns "" when no ancestor carries a description, which is a real case and
    must not produce a dangling "Accepted shapes:".
    """
    if err.validator not in _SHAPE_VALIDATORS:
        return ""
    raw = _owning_description(schema, err.absolute_path)
    text = " ".join(raw.split())
    if not text:
        return ""
    if len(text) > _SHAPE_DESCRIPTION_BUDGET:
        cut = text.rfind(". ", 0, _SHAPE_DESCRIPTION_BUDGET)
        text = (
            text[: cut + 1]
            if cut > 0
            else text[:_SHAPE_DESCRIPTION_BUDGET].rstrip() + "…"
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
    # 🔴 Validate against the draft the CONTRACT declares, not a hardcoded newer
    # one. All five pinned contracts say `draft-07`; this was pinned to
    # Draft202012Validator, and `dependencies` was REMOVED in 2020-12 (split into
    # `dependentRequired`). So a draft-07 `dependencies` rule was not merely
    # unenforced — the keyword did not exist for us, and an unknown keyword is
    # ignored in silence.
    #
    # Measured when backend added `fitAxis.dependencies: {keyword_scores:
    # [text_fields]}` — authoring `keyword_scores` alone inherits the vendored
    # `["project_description", "project_type"]` through a shallow merge and scores
    # zero, so the pair is a correctness rule, not a style one. They chose
    # ENFORCEMENT over prose precisely so our prompt truncation could not drop it;
    # it was dropped here instead. Their ajv (draft-07) does reject it, so the
    # gateway gates held — what we lost was the in-session catch, i.e. the
    # `_shape_hint` teaching layer never gets to explain it.
    #
    # `validator_for` honours `$schema` and falls back to the latest draft when a
    # schema declares none, so this is strictly more correct for every contract.
    # Verified behaviour-preserving before switching: across the four production
    # configs plus four synthetic edges, 7 of 8 verdicts are byte-identical and
    # the only difference is the intended pair rule. None of the five contracts
    # uses a construct whose meaning differs between the drafts.
    validator = validators.validator_for(schema)(schema)
    issues = []
    for err in validator.iter_errors(instance):
        message = err.message
        hint = _shape_hint(schema, err)
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
