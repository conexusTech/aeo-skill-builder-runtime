"""draftConfig construction + RFC 6902 delta computation (PRD §4/§6).

`draftConfig` is built incrementally. Every revision is emitted as a STATE_DELTA
(an RFC 6902 JSON Patch the gateway applies + persists); a STATE_SNAPSHOT is
used only when a delta base is ambiguous — the first proposal, or a large
restructure (PRD §4). This module produces the patches; the emitter
(app.skill_builder.protocol.agui) puts them on the wire.

Patches are computed with `jsonpatch` (the RFC 6902 reference implementation)
rather than hand-rolled ops, so array moves and nested edits are correct.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import jsonpatch

logger = logging.getLogger(__name__)

# Slug convention (PRD §6, Q2): <vertical>-prospect-scanner. The gateway
# resolves collisions (auto-suffix); we only propose.
_SLUG_SUFFIX = "prospect-scanner"
_NON_SLUG = re.compile(r"[^a-z0-9]+")

# The ratified skill-builder-config contract version (its `version` is a `const`,
# so this is not a free-form draft marker — it must match the schema we validate
# against, and a bump is a coordinated cross-repo change).
CONTRACT_VERSION = "1.0"

# `organizations.lead_type` / `organization_lead_type_enum` — R13 match dimension
# 2 of 3. The gateway's runtime-context returns this column directly, so a
# conforming payload can only carry one of these or null.
LEAD_TYPES = frozenset({"A", "B", "MIXED"})

# What a BUILD session proposes for the two run-shaping knobs the model
# otherwise omits (product call, Leo 2026-08-20).
#
# Why it is here and not left to the prompt: every `geographyTargeting` knob is
# optional and the scanner carries its own fallback for each, so an unauthored
# knob is invisible -- the operator is never shown a number and never gets the
# chance to change one. The schema's sibling knobs advertise their runtime
# defaults in their descriptions ("runtime default 10", "runtime default 12",
# "`metro` (runtime default)"); this one does not, so the model has nothing to
# anchor on and omits it. Injecting the value makes it appear in the proposal's
# STATE_DELTA, which is the only place an operator can review and revise it.
#
# It is a CEILING, not a target: `geo_loop.discover_in_area` runs another sweep
# only while the in-area count is short of `discovery.target_prospects` (a
# FLOOR), so this raises the permitted depth rather than forcing four sweeps.
# The scanner's own fallback is 2.
INITIAL_MAX_DISCOVERY_ROUNDS = 4

# The run's cumulative prospect ceiling, and the knob that makes the round cap
# above safe to raise. NOT derived from the round count: `capped_discover`
# applies the ceiling INSIDE the round loop (`budget.take(found)` returns only
# the kept subset), and once exhausted it returns `[]`, which
# `discover_in_area` reads as an exhausted search and breaks the loop. So four
# rounds cannot cost more verification than two -- at most two extra discovery
# sweeps, and only while the ceiling still has room.
#
# 150 is backend's MEASURED production value, not a guess (skill-update
# followups tracker, 2026-08-17). Verification is `(N/8) x 80s` at
# SCANNER_PHASE_CONCURRENCY=8 -- 1009s measured against 1000s predicted for
# 100 -- so the budget is `499s discovery + 1500s verify(150) + 945s
# validate(94, the ~63% that pass metro geo) + 500s contacts(50)` = 57 min,
# 64% of the 5400s job deadline. Unbounded is 82 min / 91%, and the run that
# motivated the ceiling (0a85e4bd) discovered 262, spent 49 min verifying, and
# was killed by the deadline having scored nothing.
#
# Re-derive rather than carry this forward blind: it moves with
# SCANNER_PHASE_CONCURRENCY, the geo-reject rate, and the deadline. Backend's
# runs log `prospect ceiling: <n> for this run`, which is where the live value
# is observable.
INITIAL_MAX_PROSPECTS = 150

#: Section -> (path within it, value) seeded when a build session omits the key.
#: A table rather than two branches because the nesting differs -- the round cap
#: is under `targeting`, the ceiling is a direct `discovery` key -- and a table
#: keeps "which sections are touched at all" answerable in one place.
_SEEDED_DEFAULTS: dict[str, tuple[tuple[str, ...], int]] = {
    "geography": (("targeting", "max_discovery_rounds"), INITIAL_MAX_DISCOVERY_ROUNDS),
    "discovery": (("max_prospects",), INITIAL_MAX_PROSPECTS),
}


def build_slug(vertical: str | None) -> str:
    """Propose a kebab-case slug from the vertical (PRD §6). Falls back to a
    generic slug when the vertical is unknown, so the draft always has a
    schema-valid slug to show."""
    base = _NON_SLUG.sub("-", (vertical or "").strip().lower()).strip("-")
    return f"{base}-{_SLUG_SUFFIX}" if base else _SLUG_SUFFIX


def skeleton(
    *,
    name: str,
    vertical: str | None,
    lead_type: str | None,
    product_description: str,
    type_: str | None = None,
) -> dict[str, Any]:
    """Initial draftConfig from the customer context (PRD §7.1).

    `lead_type` must be an `organizations.lead_type` ENUM value (`A` / `B` /
    `MIXED`); anything else is dropped rather than emitted. The gateway's
    runtime-context returns that column directly, so a conforming payload can
    only carry an enum value or null — but `CustomerContext.lead_type` digs
    `onboarding_data.lead_type` and `lead_scoring.lead_type` as fallbacks, and
    those are free-text. Guarding here means a prose fallback degrades to an
    omitted key instead of a config that fails the contract enum.

    Carries the catalog metadata plus empty objects for the five authoring
    sections, so the first STATE_SNAPSHOT is already valid against the ratified
    config contract and the operator sees the plan's skeleton. Section CONTENT
    is filled in later, one accepted section at a time.

    **Omit what isn't known yet; never placeholder it** (authoring rule ratified
    with the config contract on 2026-08-03). The contract's root envelope is
    `additionalProperties: false` and `type` / `lead_type` are enum-constrained,
    so a placeholder like `""` or a guessed skill type is a hard validation
    failure from the very first snapshot — and, worse, a placeholder that
    happened to pass would travel all the way to finalize looking legitimate.
    An absent key is the honest representation of "not decided yet".
    """
    config: dict[str, Any] = {
        # `version` names the CONTRACT rather than the draft's content, so it is
        # known before anything is authored and is the one field safe to carry
        # from the first snapshot.
        "version": CONTRACT_VERSION,
        "name": name,
        "slug": build_slug(vertical),
        "run_parameters": {},
        # The five authoring sections (NOT runtime phases — see the contract's
        # root $comment). `execution_phases` is deliberately absent: those are
        # runtime scanner step ids, and the gateway strips the field before the
        # agent ever sees a config, so it is not ours to author.
        "geography": {},
        "discovery": {},
        "validation": {},
        "contacts": {},
        "scoring": {},
    }
    if type_ is not None:
        config["type"] = type_
    if vertical:
        config["vertical"] = vertical
    if lead_type in LEAD_TYPES:
        config["lead_type"] = lead_type
    if product_description:
        config["product_description"] = product_description
    return config


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """RFC 6902 patch turning `before` into `after` (the STATE_DELTA payload)."""
    return cast(list[dict[str, Any]], jsonpatch.make_patch(before, after).patch)


def apply(config: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply an RFC 6902 patch, returning a NEW dict (never mutates input).

    Used to keep the agent's local copy of draftConfig in sync with what the
    gateway will hold after it applies the same delta — so the next delta is
    computed against the correct base.
    """
    return cast(dict[str, Any], jsonpatch.apply_patch(config, patch, in_place=False))


def set_section(
    config: dict[str, Any],
    phase: str,
    section: dict[str, Any],
    *,
    edit_mode: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Set `config[phase]` to `section`, returning (new_config, patch).

    This is the per-phase revision primitive (PRD §7.2): propose or revise one
    section, get back the updated local draft plus the RFC 6902 delta to emit.

    Self-wrapping is normalised here because this is the ONE owner of a section
    write, so every path is covered by fixing it once. `_SEEDED_DEFAULTS` is
    applied here for the same reason: a section write REPLACES the section
    wholesale, so seeding those knobs in `skeleton()` would be discarded by the
    first proposal of their section.

    `edit_mode` suppresses the seed. An edit session's config comes from a
    finalized skill that is already running; injecting a knob it deliberately
    omits would move a live skill from the scanner's 2 rounds to 4, or impose a
    ceiling on a run that has never had one, because someone opened a section to
    change a market.
    """
    section = _unwrap_self_named(phase, section)
    if not edit_mode:
        section = _with_seeded_defaults(phase, section)
    after = apply(config, [{"op": "replace" if phase in config else "add",
                            "path": f"/{phase}", "value": section}])
    return after, diff(config, after)


def _with_seeded_defaults(phase: str, section: dict[str, Any]) -> dict[str, Any]:
    """Seed this section's `_SEEDED_DEFAULTS` knob when the model omitted it.

    Fires on EVERY write of that section in a build session, not only the first,
    and that is the point. Gating it on "the section was empty" would look more
    conservative and would silently regress: the model re-proposes a section from
    what IT authored, and it never saw the injected value, so an unrelated later
    revision (change a home market, keep everything else) would drop the knob and
    the run would quietly fall back to the engine's own default. Absent therefore
    always means the seeded value, and the operator changes it by SAYING a number
    -- which the model then authors, and an authored value is never overwritten.

    Omission was never expressive anyway: every knob here is optional with a
    runtime fallback, so leaving `max_discovery_rounds` out means 2 rounds, and
    leaving `max_prospects` out means the unbounded 91%-of-deadline path.

    Returns copies rather than mutating -- the caller's contract is that a section
    body is not modified in place.
    """
    seed = _SEEDED_DEFAULTS.get(phase)
    if seed is None:
        return section
    path, value = seed
    *containers, key = path
    # Copy every level on the way DOWN. An earlier version built the copies on
    # the way back up, which silently aliased the one-element path
    # (`discovery.max_prospects`): with no container to rebuild, `node` was the
    # caller's own dict and the seed was written straight into it. The existing
    # `set_section` test then passed for the wrong reason -- it compared the new
    # config against the very dict that had just been mutated.
    root: dict[str, Any] = dict(section)
    node = root
    for name in containers:
        child = node.get(name)
        node[name] = dict(child) if isinstance(child, dict) else {}
        node = node[name]
    if key in node:
        return section
    node[key] = value
    return root


def _unwrap_self_named(phase: str, section: dict[str, Any]) -> dict[str, Any]:
    """Undo a section body wrapped in its own name.

    A live turn had the model return `{"geography": {...}}` as the body of the
    geography section, which lands as `draftConfig.geography.geography`. That
    shape **validates** — sections are `additionalProperties: true` — so the
    config validator, the R12 lint, the patch application and the emitted delta
    all pass, and the corruption is visible only to someone reading a payload
    by eye.

    It was fixed in the tool description first. That was not enough, and the
    reason is worth keeping: the failure does not raise, so "wait and see if it
    recurs" makes RECURRENCE THE DETECTOR for something that is silent by
    construction. Prompt wording is a request; this is the check.

    Deliberately narrow — unwrap only the unambiguous shape (exactly one key,
    equal to the phase, holding an object). A partial body like
    `{"targeting": {...}}` has one key that is NOT the phase and is untouched;
    a real body has several. Logged at WARNING rather than repaired silently,
    because a model that needs this repair is worth noticing.
    """
    if len(section) == 1:
        (only_key,) = section
        inner = section[only_key]
        if only_key == phase and isinstance(inner, dict):
            logger.warning(
                "section %r was wrapped in its own name; unwrapping "
                "(would have produced %s.%s)", phase, phase, phase
            )
            return cast("dict[str, Any]", inner)
    return section
