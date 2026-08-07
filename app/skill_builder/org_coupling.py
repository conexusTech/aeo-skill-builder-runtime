"""R12 org-coupling lint — the gateway's verdict, reproduced before we emit.

A builder-produced config must describe a *vertical*, not an organization, so
anywhere it carries org-specific data (home markets, ICP attributes,
disqualifiers, decision-maker titles) it must reference a well-known context
field resolved per-org at scan time instead of baking in the commissioning org's
literal values. That reference is what makes one skill serve many orgs.

**Why a second implementation rather than a round trip.** The lint is
deliberately NOT JSON Schema — schema can say "this key holds an object", not "a
literal here is legal only when a sibling `context_ref` explains it". So section
internals are `additionalProperties: true`, and a wrong binding passes the config
schema, passes our conformance run, and fails only at the gateway's test-run or
finalize gate. That is the worst place to learn it: the whole conversation has
been invested, and the repair loop has the least to work with. The gateway wrote
`lintOrgCoupling` as a pure function specifically so we could run the same check
first, and published its `(section, key) → context_ref` mapping (thread #13.6)
so we could do it without reading their source.

**This mirrors `org-coupling.lint.ts` deliberately, including the parts that look
arbitrary.** `{}` is not "empty" (only None / empty list / blank string are);
a binding's own `default` is not descended into; a location already reported by
the enumerated pass is not reported twice by the recursive one. Divergence here
does not produce a different opinion, it produces a verdict that disagrees with
the one that actually gates finalize — which is worse than not checking.

Locations are **dotted** (`geography.home_markets`), matching the gateway's
coupling issues so an operator comparing our pre-emit message against a gateway
rejection sees the same string. Note their schema errors use a different format
(`geography/home_markets`), so their own `issues[]` carries both; ours does too,
for the same reason.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from app.skill_builder import contracts
from app.skill_builder.validator import ValidationIssue


def _is_binding(value: Any) -> TypeGuard[dict[str, Any]]:
    """A `{context_ref, default?}` marker per the config contract.

    A TypeGuard rather than a plain bool so the callers' `.get("context_ref")`
    type-checks — the alternative was a cast, which would assert the same thing
    without checking it.
    """
    return isinstance(value, dict) and "context_ref" in value


def _is_empty(value: Any) -> bool:
    """Mirrors the gateway's `isEmpty`. An empty dict is deliberately NOT empty."""
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, str):
        return value.strip() == ""
    return False


def lint_org_coupling(config: dict[str, Any]) -> list[ValidationIssue]:
    """Report org-coupling violations. Pure — no DB, no org context.

    Empty list means the gateway's R12 lint should also pass. Judged only against
    the published vocabulary, which is what makes the verdict reproducible.
    """
    issues: list[ValidationIssue] = []
    valid_keys = contracts.context_field_keys()

    for position in contracts.config_positions():
        section_value = config.get(position.section)
        if not isinstance(section_value, dict):
            continue  # Section absent or not yet authored — not this lint's business.

        value = section_value.get(position.key)
        # Absent is fine: R12 governs how a value is expressed, never whether the
        # agent had to author it. Requiredness is the schema's job.
        if _is_empty(value):
            continue

        location = f"{position.section}.{position.key}"

        if not _is_binding(value):
            issues.append(
                ValidationIssue(
                    location=location,
                    message=(
                        "Org-specific value must bind to a context field instead of "
                        f'a literal. Replace it with {{"context_ref": '
                        f'"{position.context_ref}"}}, optionally keeping the current '
                        'value as an adjacent "default".'
                    ),
                )
            )
            continue

        ref = value.get("context_ref")
        if not isinstance(ref, str) or ref not in valid_keys:
            # A typo'd ref is worse than a literal: it resolves to nothing at scan
            # time, so targeting silently narrows or widens instead of failing.
            issues.append(
                ValidationIssue(
                    location=f"{location}.context_ref",
                    message=(
                        f"Unknown context field {ref!r}. Must be one of the published "
                        f'keys (e.g. "{position.context_ref}"). Keys are unprefixed — '
                        f'"customer.{position.context_ref}" is not valid.'
                    ),
                )
            )

    _collect_runtime_populated_issues(config, issues)
    _collect_stray_refs(config, [], issues, valid_keys)
    return issues


def _collect_runtime_populated_issues(
    config: dict[str, Any], issues: list[ValidationIssue]
) -> None:
    """Reject any authored value at a runtime-populated position.

    The half of the fix we did not propose and that actually closes the hole: a
    fan-out in the runtime creates a correct path, but the incorrect one still
    passes — and the incorrect one is what a model reaches for, because a literal
    list is the obvious way to express "firms we already know".

    Rejects a BINDING here too, not just a literal. The rule is "not authored at
    all": the runtime supplies the value from `populated_from`, so even a correct
    `context_ref` is a second, competing source for the same field.
    """
    for position in contracts.runtime_populated_positions():
        section_value = config.get(position.section)
        if not isinstance(section_value, dict):
            continue
        collection = section_value.get(position.collection)
        if not isinstance(collection, dict):
            continue
        for key, entry in collection.items():
            if not isinstance(entry, dict):
                continue
            value = entry.get(position.leaf)
            if _is_empty(value):
                continue
            issues.append(
                ValidationIssue(
                    location=(
                        f"{position.section}.{position.collection}.{key}.{position.leaf}"
                    ),
                    message=(
                        f"Do not author {position.leaf!r} — the scan runtime populates "
                        f"it per-org from {position.populated_from!r}. "
                        f"Author {position.section}.{position.populated_from} as "
                        f'{{"context_ref": "{position.populated_from}"}} instead and '
                        f"omit this field. {position.reason}"
                    ),
                )
            )


def _collect_stray_refs(
    node: Any,
    path: list[str],
    issues: list[ValidationIssue],
    valid_keys: frozenset[str],
) -> None:
    """Validate EVERY `context_ref` in the document, not just the enumerated ones.

    The model may bind fields nobody enumerated, and an unresolvable ref is a
    defect wherever it appears — it is a silent no-op at scan time.
    """
    if isinstance(node, list):
        for index, item in enumerate(node):
            _collect_stray_refs(item, [*path, str(index)], issues, valid_keys)
        return
    if not isinstance(node, dict):
        return

    if _is_binding(node):
        location = ".".join([*path, "context_ref"])
        if any(issue.location == location for issue in issues):
            return  # Already reported by the enumerated pass above.
        ref = node.get("context_ref")
        if not isinstance(ref, str) or ref not in valid_keys:
            issues.append(
                ValidationIssue(
                    location=location,
                    message=(
                        f"Unknown context field {ref!r}. Must be one of the published "
                        "keys. Keys are unprefixed, and dotted paths or "
                        '"{{template}}" strings are not context refs.'
                    ),
                )
            )
        return  # Don't descend into a binding's own `default`.

    for key, value in node.items():
        _collect_stray_refs(value, [*path, key], issues, valid_keys)
