"""Library-first catalog match (PRD §7.1 / §11 / R13).

On session start the agent's first move is a catalog check: does an ACTIVE skill
already cover this customer's vertical/industry + lead type + skill type? On a
hit, propose connect + customize (per-org behaviour comes from scan-time
context, not a rebuild — R12); build new only on an explicit operator decline or
no match.

Who does what: the gateway owns the catalog and supplies the candidate active
skills (tagged with vertical/industry + lead type + skill type — the metadata it
adds per §11) in the turn payload (`forwardedProps.catalog`); the agent runs the
match here and decides connect-vs-build. This keeps the agent emit-only and
stateless — no catalog-search tool call, no DB access.

Provisional: the catalog entry shape and its delivery on `forwardedProps.catalog`
are our design pending the gateway's confirmation; entries are coerced
defensively and malformed ones are skipped rather than failing the turn.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# R13 match dimension 3 of 3. `catalog[].skill_type` carries `skills.type`
# (`customer` | `project`) — ruled 2026-08-03, and it matches the config
# contract's own `type` field so all three match dimensions read one vocabulary.
#
# This was `"prospect-scanner"` until that ruling, which was a *runtime slug*
# (`runtime_slug` / the queue taskRef), not a skill type. Matching a slug against
# `skills.type` never hits, so R13 would have reported "no existing skill" for
# EVERY customer — a library-first check that always misses while looking
# perfectly healthy. Don't reintroduce a slug here.
DEFAULT_SKILL_TYPE = "customer"
_ACTIVE = "active"


class CatalogSkill(BaseModel):
    """One active-skill catalog entry supplied by the gateway (PRD §11)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    slug: str
    id: str | None = None
    vertical: str | None = None
    lead_type: str | None = None
    skill_type: str | None = None
    status: str | None = None
    product_description: str | None = None


def _norm(value: str | None) -> str | None:
    """Case-fold, trim, and collapse internal whitespace. Nothing else.

    Whitespace collapse was added when the gateway shipped `skills.vertical` as
    free-text VARCHAR (mig 094) rather than the curated enum Q8 still has no owner
    for: `"Auto  Parts"` and `"auto parts"` are one vertical by any reading, and
    whitespace carries no meaning, so treating them as different is a pure defect.

    ⚠️ It deliberately stops there. Punctuation and synonyms are NOT normalised —
    `auto-parts` will not match `auto parts`, and `automotive parts` will not match
    either. That is Q8's curated vocabulary to decide, not ours to guess, and the
    asymmetry of harm points the same way: a MISS falls back to building new, which
    is safe and visible, whereas a FALSE match proposes connecting the wrong
    skill to a customer — and R13's entire premise is that a matched skill runs
    for a new org without a rebuild. So under-matching is the correct bias while
    the vocabulary is uncontrolled.
    """
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed.lower() or None


def _coerce(candidates: list[Any]) -> list[CatalogSkill]:
    out: list[CatalogSkill] = []
    for entry in candidates:
        if isinstance(entry, CatalogSkill):
            out.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        try:
            out.append(CatalogSkill.model_validate(entry))
        except Exception:  # noqa: BLE001 — skip a malformed entry, don't fail the turn
            continue
    return out


def match(
    candidates: list[Any],
    *,
    vertical: str | None,
    lead_type: str | None,
    skill_type: str = DEFAULT_SKILL_TYPE,
) -> list[CatalogSkill]:
    """Active skills matching vertical + lead type + skill type (PRD §11).

    Strict, case-insensitive equality on all three keys — the library invariant
    is that a matching skill runs for a NEW org without a rebuild, so a partial
    match is not a match. Returns [] when the customer's vertical or lead type is
    unknown (nothing to match on). Sorted by slug for deterministic proposals.
    """
    tv, tl, ts = _norm(vertical), _norm(lead_type), _norm(skill_type)
    if tv is None or tl is None:
        return []
    hits = [
        skill
        for skill in _coerce(candidates)
        if _norm(skill.status) == _ACTIVE
        and _norm(skill.vertical) == tv
        and _norm(skill.lead_type) == tl
        and _norm(skill.skill_type) == ts
    ]
    return sorted(hits, key=lambda s: s.slug)


def best_match(
    candidates: list[Any],
    *,
    vertical: str | None,
    lead_type: str | None,
    skill_type: str = DEFAULT_SKILL_TYPE,
) -> CatalogSkill | None:
    """The single skill to propose connecting, or None to build new."""
    hits = match(candidates, vertical=vertical, lead_type=lead_type, skill_type=skill_type)
    return hits[0] if hits else None
