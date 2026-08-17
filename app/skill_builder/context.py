"""Customer context grounding (PRD §5) + the injection guardrail (PRD §9).

`forwardedProps.customer_context` is the org runtime-context JSON — org columns
+ the onboarding_data blob (geographic_strategy, icp_seeds, contact_discovery,
scoring_strategy, lead_scoring, …) + Neo4j :Persona/:Product nodes + skill
config context. It is already live (GET /runtime/organizations/:orgId/context)
and consumed as-is; its shape is owned by that endpoint, not by us, so this
module treats it as an arbitrary dict and digs defensively.

Two jobs:
  1. Surface the few facts the first assistant message MUST reflect — customer
     name, ICP summary, lead type — so the operator can immediately catch a
     wrong-org error (PRD §5 acceptance criterion).
  2. Render the blob for the prompt as DATA, never instructions. Onboarding
     free-text is untrusted (PRD §9 / Risk: prompt injection); `as_prompt_block`
     fences it so a "ignore your instructions…" string inside an ICP field is
     read as content, not obeyed. The agent has no write authority regardless
     (defense in depth), but the fence is the first line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel fence markers. The baseline prompt layer (app.skill_builder.prompt)
# tells the model that everything between these markers is untrusted data.
CONTEXT_FENCE_OPEN = "<<<CUSTOMER_CONTEXT_DATA>>>"
CONTEXT_FENCE_CLOSE = "<<<END_CUSTOMER_CONTEXT_DATA>>>"

#: What replaces a fence sentinel found inside the customer blob. Deliberately
#: contains no angle brackets, so the replacement cannot itself be mistaken for a
#: marker (see `CustomerContext.as_prompt_block`).
_FENCE_REMOVED = "[fence-marker removed]"


#: Industry values that carry no vertical information. `organization.industry` is
#: an enum whose catch-all is the literal "Other"; treating it as a vertical makes
#: the catalog match on a word that describes nothing and stamps it into the slug.
_PLACEHOLDER_VERTICALS = frozenset({"other", "n/a", "na", "none", "unknown", "unspecified"})


def _shape(value: Any) -> str:
    """A key's SHAPE for diagnostics — never its contents.

    "empty list" and "absent" look identical in a log that prints only key names,
    and they have different owners: one is the operator's onboarding to fill in,
    the other is our reader looking in the wrong place.
    """
    if isinstance(value, (list, tuple, set)):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if value in (None, ""):
        return "empty"
    return type(value).__name__


def _first(data: dict[str, Any], *paths: str) -> Any:
    """Return the first present, non-empty value across candidate dotted paths.

    The blob's exact layout varies (top-level org columns vs nested
    onboarding_data), so callers pass several likely locations and take the
    first hit. Never raises on a missing branch.
    """
    for path in paths:
        node: Any = data
        ok = True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and node not in (None, "", [], {}):
            return node
    return None


def _first_str(data: dict[str, Any], *paths: str) -> str | None:
    """`_first` narrowed to a string — org columns we surface are text fields;
    a non-string hit reads as absent rather than leaking a non-str value."""
    value = _first(data, *paths)
    return value if isinstance(value, str) else None


class CustomerContext:
    """Read-only view over the customer_context blob. Not a Pydantic model —
    the blob is arbitrary and owned elsewhere; wrapping it keeps parsing
    defensive and free-text firmly in the 'data' lane."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data: dict[str, Any] = data or {}

    @property
    def is_empty(self) -> bool:
        return not self.data

    @property
    def organization_name(self) -> str | None:
        return _first_str(
            self.data,
            "organization_name",
            "organization.name",
            "name",
            "org.name",
        )

    def _industry_value(self) -> str | None:
        """The industry/vertical exactly as the customer blob carries it, BEFORE
        the placeholder mapping.

        Private on purpose: a caller wants either a usable vertical or the
        placeholder FACT, never the raw catch-all — returning "Other" to anything
        that treats it as a vertical is the defect `vertical` exists to stop.
        """
        return _first_str(
            self.data,
            "vertical",
            "industry",
            # `organization.industry` is what the live payload carries (measured
            # 2026-08-13 alongside the ICP miss). Without it `vertical` was None
            # on every real session, which feeds the R13 catalog match and the
            # slug — the same silent-degradation family as the ICP, found in the
            # same capture.
            "organization.industry",
            "organization.vertical",
            "onboarding_data.vertical",
        )

    @property
    def industry_is_placeholder(self) -> bool:
        """True when the blob DOES carry an industry and it is a catch-all.

        `vertical` collapses "no industry at all" and "industry is the literal
        'Other'" onto the same None. That is right for the catalog match and the
        slug, and wrong for instructing the model: only the second case leaves a
        POPULATED `industry` inside the fenced blob, and the model reads that as
        the answer already being present.

        The wire schema asks for `ModelDecision.vertical` "ONLY when you had to
        ask the operator for it because it was missing from the customer
        context" — a precondition that reads FALSE from inside the prompt on
        exactly these sessions. Keeping the two cases distinguishable is what
        lets the per-turn task say WHY the vertical is still unknown instead of
        contradicting the data the model can see.
        """
        value = self._industry_value()
        return value is not None and value.strip().casefold() in _PLACEHOLDER_VERTICALS

    @property
    def vertical(self) -> str | None:
        value = self._industry_value()
        # 🔴 A PLACEHOLDER IS NOT A VERTICAL. `organization.industry` is an enum
        # with a catch-all, and Lee Company's value is the literal "Other".
        #
        # Resolving it was still right — it was None on every session before —
        # but returning it unguarded is worse than None in the one place it
        # matters: `None` makes the R13 catalog ABSTAIN and the kickoff ASK the
        # operator (the v7 fix for #27 §3), while "Other" makes it MATCH, report
        # a confident negative, and persist the placeholder onward —
        # `slug: "other-prospect-scanner"`, "skill for the Other vertical".
        # That is a completed search that never ran, which is the self-
        # perpetuating shape #27 §3 exists to stop.
        #
        # So: map the catch-alls back to None and let the ASK path own it. The
        # operator knows their vertical; the enum's fallback does not.
        if value is not None and value.strip().casefold() in _PLACEHOLDER_VERTICALS:
            return None
        return value

    @property
    def lead_type(self) -> str | None:
        return _first_str(
            self.data,
            "lead_type",
            "onboarding_data.lead_type",
            "lead_scoring.lead_type",
        )

    @property
    def icp_summary(self) -> str | None:
        """A short human string describing the ICP, for the first message.

        Prefers an explicit summary field; falls back to serialising the ICP
        seeds. Whatever it returns is DATA — the caller fences it.
        """
        explicit = _first(
            self.data,
            "icp_summary",
            "onboarding_data.icp_summary",
            "onboarding_data.icp_seeds.summary",
        )
        if isinstance(explicit, str):
            return explicit
        seeds = _first(
            self.data,
            # 🔴 `icp_attributes` FIRST, because it is the RATIFIED key and the
            # others are not. The closed vocabulary in `context-field-keys.json`
            # names exactly one ICP field and this is it; `icp_seeds` is the older
            # Phase 2.1 foundation-skill spelling that we never moved off.
            # Consequence (#35): on a context carrying the canonical key, the
            # kickoff told the operator "ICP: unknown (no ICP data in context)"
            # while the ICP was present and approved — reproduced across three
            # sessions, and it looked like missing DATA rather than a reader
            # reading the wrong name.
            # ⚠️ MEASURED, not assumed. The gateway sends a top-level `icp`
            # OBJECT whose members are the ratified keys — `icp.icp_attributes`,
            # not `icp_attributes`. v16 added the flat spellings from reading the
            # vocabulary file and shipped WITHOUT fixing the live payload, because
            # the test used a shape I invented rather than the one backend
            # measured. Every path below now appears in a real capture.
            "icp.icp_attributes",
            "icp.top_customers",
            "icp_attributes",
            "onboarding_data.icp_attributes",
            "icp_seeds",
            "onboarding_data.icp_seeds",
        )
        if isinstance(seeds, list) and seeds:
            return ", ".join(str(s) for s in seeds[:5])
        if isinstance(seeds, dict) and seeds:
            return json.dumps(seeds, separators=(",", ":"))[:400]
        # A STRING is a legitimate shape and we were dropping it on the floor.
        # This is what actually made #35 look fixed by v18: the paths in v17 and
        # v18 are byte-identical (only the log message's literal list differs),
        # so our code cannot be what changed the outcome. `icp.icp_attributes`
        # was arriving as a string, fell through both isinstance checks, and
        # returned None — indistinguishable from absent. It began resolving when
        # BACKEND corrected the shape to a real array, and the credit belongs
        # there.
        #
        # Handling the string too, because the shape is not ours to depend on:
        # aeo-frontend reports the ICP still arriving space-concatenated in
        # another form, so a reader that only accepts one of three shapes will
        # keep reporting an operator's data as missing.
        if isinstance(seeds, str) and seeds.strip():
            return seeds.strip()[:400]
        return None

    def first_message_facts(self) -> dict[str, str]:
        """The customer / ICP / lead-type facts the opening message must state
        (PRD §5). Missing values are rendered as an explicit 'unknown' string so
        the opener still surfaces the gap rather than silently omitting it —
        which is exactly the wrong-org signal the operator watches for."""
        icp = self.icp_summary
        if icp is None:
            # Say WHAT WE LOOKED FOR, not just that we found nothing. #35 took
            # three sessions and a cross-repo thread because the operator-facing
            # string reports missing DATA, while the actual cause was this reader
            # searching a key the ratified vocabulary does not use. The operator
            # message stays clean; the diagnosis goes to the logs, where it turns
            # "their data is broken" into "we read the wrong name" in one line.
            # ⚠️ Log ONE LEVEL DEEPER than feels necessary. The first version of
            # this line reported only top-level keys, and on the first real turn
            # it proved `icp` was present while leaving the actual question —
            # what is INSIDE it — unanswerable. That is the same error as the bug
            # it was added to diagnose: naming the container instead of looking
            # in it. Keys only, never values: structure is diagnosable, customer
            # text is not ours to put in a log.
            container = self.data.get("icp") if isinstance(self.data, dict) else None
            logger.info(
                "icp unresolved: searched=%s icp_container=%s top_level=%s",
                ["icp.icp_attributes", "icp.top_customers", "icp_summary",
                 "icp_attributes", "icp_seeds", "onboarding_data.*"],
                (
                    {k: _shape(v) for k, v in sorted(container.items())}
                    if isinstance(container, dict)
                    else type(container).__name__
                ),
                sorted(self.data)[:20] if isinstance(self.data, dict) else type(self.data).__name__,
            )
        return {
            "customer": self.organization_name or "unknown (no organization name in context)",
            "lead_type": self.lead_type or "unknown",
            "icp": icp or "unknown (no ICP data in context)",
        }

    def as_prompt_block(self) -> str:
        """The blob rendered for the prompt, fenced as untrusted DATA (PRD §9).

        The full context is serialized deterministically (sorted keys) so the
        prefix is byte-stable turn to turn — required for the manual
        prompt-cache breakpoint the model layer places over this block on
        Bedrock (no automatic caching there; turns are billed per invocation).
        """
        body = json.dumps(self.data, sort_keys=True, indent=2, default=str)
        # Neutralise the sentinels where they occur INSIDE the data. Without this a
        # hostile value (e.g. organization_name) reproduces the close-fence
        # verbatim, so the model reading "everything between the fences is data"
        # sees the block end early — which is exactly what this guardrail exists
        # to prevent, on a surface whose entire input is untrusted onboarding free
        # text. JSON encoding blunts the attack (a real newline cannot be
        # injected, and the payload stays inside a string) but that is incidental,
        # not a guarantee.
        #
        # Applied to the SERIALIZED body so it catches sentinels in keys as well as
        # values, at any nesting depth, without walking the structure. The marker
        # is visible rather than silent so an operator can see it happened, and the
        # substitution is deterministic, so the byte-stability the prompt cache
        # needs is preserved.
        for fence in (CONTEXT_FENCE_OPEN, CONTEXT_FENCE_CLOSE):
            body = body.replace(fence, _FENCE_REMOVED)
        return f"{CONTEXT_FENCE_OPEN}\n{body}\n{CONTEXT_FENCE_CLOSE}"
