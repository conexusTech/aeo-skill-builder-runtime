"""Our port of the gateway's unfilled-authoring lint.

Mirrors `aeo-backend/src/backend/skills/config/authoring-placeholders.lint.ts`,
which backend built on 2026-08-10 after a real model-backed conversation (#27)
produced two defects that **passed every gate either side had**: strict schema
and the R12 lint were both clean on the persisted config.

Both live in plain `string` values, which is why nothing saw them:

1. **Unfilled bracket placeholders** — observed verbatim:
   ``"best {market} companies offering [product/service category] compared"``.
   The bracketed text reaches the search model literally.

2. **A `context_ref` written INLINE in a string** — observed verbatim:
   ``"alternatives to {context_ref:competitors} for businesses in {market}"``.
   A binding is an OBJECT (``{"context_ref": "..."}``); the scanner's
   ``resolve()`` walks for those objects and never inspects string contents. So
   the org's competitors are never substituted and the literal text is searched.
   This is the more dangerous of the two precisely because it looks *correct* —
   right vocabulary, wrong shape — whereas a bracket is obviously unfinished to
   anyone reading the config.

**Why we carry a second implementation at all**, given the gateway already gates
on theirs: the same reason `org_coupling` exists. The agent must be able to
reproduce their verdict BEFORE it emits, so the model repairs in-conversation
rather than the operator investing a whole session and hitting a refusal at
finalize.

⚠️ **Divergence is worse than absence here.** A port that disagrees with the
lint actually gating finalize produces a verdict that is confidently wrong, so
the regexes and the report order below are copied from their implementation
rather than re-derived — including matching inline refs BEFORE brackets, since
one string can carry both and the inline case should lead.

Their issues carry ``kind: 'other'`` (the published contract declares exactly
three kinds and `other` is the documented catch-all). Our `ValidationIssue` has
no `kind` — it is a gateway wire concern — so we mirror location and message
only, which is what the model repairs from.
"""

from __future__ import annotations

import re
from typing import Any

from app.skill_builder.validator import ValidationIssue

#: Anything `[like this]` — an authoring placeholder that was never filled.
_BRACKET_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,60}\]")

#: A `context_ref` used inside a string instead of as an object binding. Matches
#: the observed shape plus the spaced and quoted variants a model might equally
#: produce — the point is to catch the intent, not one spelling of it.
_INLINE_CONTEXT_REF = re.compile(
    r"\{\s*[\"']?context_ref[\"']?\s*:\s*[^}]*\}", re.IGNORECASE
)


def _walk_strings(node: Any, path: str, out: list[tuple[str, str]]) -> None:
    """Collect every string value in the config, with its dotted path."""
    if isinstance(node, str):
        out.append((path, node))
        return
    if isinstance(node, list):
        for i, v in enumerate(node):
            _walk_strings(v, f"{path}[{i}]", out)
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_strings(v, f"{path}.{k}" if path else str(k), out)


def lint_authoring_placeholders(config: dict[str, Any]) -> list[ValidationIssue]:
    """Report unfilled authoring placeholders. Pure — no DB, no org context.

    Walks **every** string in the document, not just `discovery.*.queries`. Both
    observed instances were in queries, but neither defect is specific to that
    position, and a check scoped to where a defect first appeared is the kind
    that misses its own second instance.
    """
    strings: list[tuple[str, str]] = []
    _walk_strings(config, "", strings)

    issues: list[ValidationIssue] = []
    for path, value in strings:
        # Inline refs first: a string can carry both, and this is the more
        # misleading of the two, so it should lead the report.
        inline = _INLINE_CONTEXT_REF.search(value)
        if inline:
            issues.append(
                ValidationIssue(
                    location=path,
                    message=(
                        f"contains an inline context reference ({inline.group(0)}), "
                        "which is never resolved — a context_ref must be an object "
                        'binding ({"context_ref": "..."}), not text inside a string. '
                        "As written, this literal text would be searched verbatim."
                    ),
                )
            )

        brackets = _BRACKET_PLACEHOLDER.findall(value)
        if brackets:
            issues.append(
                ValidationIssue(
                    location=path,
                    message=(
                        "contains an unfilled template placeholder "
                        f"({', '.join(brackets)}). Replace it with a concrete value, "
                        "or bind it to a context field if it is org-specific — "
                        "otherwise the bracketed text is searched literally."
                    ),
                )
            )
    return issues
