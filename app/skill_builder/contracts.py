"""Loads the five gateway-owned contracts (PRD §14) — the single swap point.

Each contract loads from its `SKILL_BUILDER_*_PATH` override when set, else the
bundled stub in `app.skill_builder.stubs`. The gateway publishes real, versioned
contracts first (they block the build per §14); pointing the env var at the
published file swaps the real contract in with zero code change (§15).

Results are cached per resolved path so a turn doesn't re-read from disk on
every invocation; a changed env var (new deploy) uses a new cache key.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

from app.config import get_settings

_STUB_DIR = Path(__file__).parent / "stubs"


@lru_cache
def _load_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"contract at {path!r} must be a JSON object, got {type(data).__name__}")
    return data


def _resolve(override: str, stub_name: str) -> str:
    """Override path if set, else the bundled stub. Returns an absolute path."""
    if override:
        return str(Path(override).expanduser())
    return str(_STUB_DIR / stub_name)


def config_schema() -> dict[str, Any]:
    """The skill-builder-config JSON Schema (contract #1)."""
    settings = get_settings()
    return _load_json(
        _resolve(settings.SKILL_BUILDER_CONFIG_SCHEMA_PATH, "config_schema.json")
    )


def context_field_keys() -> frozenset[str]:
    """The well-known context-field keys (contract #2), as a set for O(1) lookup
    by the org-coupling logic (R12).

    The ratified contract publishes the closed set twice: `key_list` as a flat
    array (authoritative for validating a `context_ref`) and `keys` as the same
    set carrying per-key metadata. Prefer `key_list`; fall back to `keys` in
    either form, since the pre-ratification stub shipped it as a bare array.
    """
    settings = get_settings()
    path = _resolve(
        settings.SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH, "context_field_keys.json"
    )
    doc = _load_json(path)
    keys: Any = doc.get("key_list")
    if keys is None:
        keys = doc.get("keys", [])
    if isinstance(keys, dict):  # metadata-map form — its keys are the vocabulary
        keys = list(keys)
    if not isinstance(keys, list):
        raise ValueError(
            "context-field-keys contract must expose 'key_list' (or 'keys') as an "
            f"array or object map, got {type(keys).__name__}"
        )
    if not keys:
        # An empty vocabulary is never legitimate, and it fails in the worst
        # available way if allowed through: the prompt's bindings layer still
        # tells the model a `context_ref` is mandatory and then offers no legal
        # key, so it must either invent one (a hard R12 lint failure) or emit a
        # bare literal (an org_coupling failure). Every draft would be rejected —
        # at test-run/finalize, with nothing wrong at load time.
        raise ValueError(f"context-field-keys contract at {path!r} has an empty vocabulary")
    return frozenset(str(k) for k in keys)


class ConfigPosition(NamedTuple):
    """One config position the R12 lint requires to be a `context_ref` binding."""

    section: str
    key: str
    context_ref: str


def config_positions() -> tuple[ConfigPosition, ...]:
    """The R12 lint's enforced `(section, key) → context_ref` bindings.

    Published in `context-field-keys.json` on 2026-08-04 at our request: the
    mapping previously existed only in the gateway's `org-coupling.lint.ts`, so
    we could not reproduce a verdict their lint was written to be reproducible.
    Two entries are not derivable from the key name (`contacts.titles` binds
    `decision_titles`), which is why guessing was never an option.

    Published order is preserved — it is a JSON array, not a set, so unlike
    `context_field_keys` it needs no sorting to be byte-stable in the cached
    prompt prefix.

    Raises if the block is missing or empty rather than degrading: without it the
    lint silently stops checking the nine positions and only catches stray refs,
    so a literal at `geography.home_markets` would pass every check we make and
    fail at the gateway's finalize gate — the exact failure this port exists to
    move earlier.
    """
    settings = get_settings()
    path = _resolve(
        settings.SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH, "context_field_keys.json"
    )
    raw = _load_json(path).get("config_positions")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"context-field-keys contract at {path!r} must expose a non-empty "
            "'config_positions' array (published 2026-08-04); without it the R12 "
            "lint cannot check the nine enforced positions"
        )
    positions = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"config_positions entry must be an object, got {entry!r}")
        try:
            positions.append(
                ConfigPosition(
                    section=str(entry["section"]),
                    key=str(entry["key"]),
                    context_ref=str(entry["context_ref"]),
                )
            )
        except KeyError as exc:
            raise ValueError(
                f"config_positions entry {entry!r} is missing {exc.args[0]!r}"
            ) from exc
    return tuple(positions)


class RuntimePopulatedPosition(NamedTuple):
    """A position the config must NOT author — the scan runtime fills it."""

    section: str
    collection: str
    leaf: str
    populated_from: str
    reason: str

    @property
    def location(self) -> str:
        """Human-readable `section.collection.*.leaf`, for messages."""
        return f"{self.section}.{self.collection}.*.{self.leaf}"


def runtime_populated_positions() -> tuple[RuntimePopulatedPosition, ...]:
    """The third kind of R12 rule (published 2026-08-04, thread #17).

    `config_positions` says "this position must bind to a context field"; this
    says "**this position must not be authored at all** — the runtime fills it".
    It exists because `discovery.sources.<key>.seed_firms` is the commissioning
    org's own customer list: authored as a literal it travels with the skill, and
    the next org to connect it searches using the first org's customers. A literal
    there passed the config schema, passed both org-coupling lints, and finalized.

    The ONE supported pattern is `<collection>.*.<leaf>`, and anything else RAISES
    rather than being skipped — a silently-unmatched pattern would make the lint
    pass by accident, which is the failure mode this rule was added to leave
    behind. An absent block is legitimate (nothing to forbid); a malformed entry
    is not.
    """
    settings = get_settings()
    path = _resolve(
        settings.SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH, "context_field_keys.json"
    )
    raw = _load_json(path).get("runtime_populated_positions", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"context-field-keys contract at {path!r} has a non-list "
            f"'runtime_populated_positions' ({type(raw).__name__})"
        )
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"runtime_populated_positions entry must be an object: {entry!r}")
        pattern = str(entry.get("path", ""))
        parts = pattern.split(".")
        if len(parts) != 3 or parts[1] != "*":
            raise ValueError(
                f"runtime_populated_positions path {pattern!r} is not the one "
                "supported pattern '<collection>.*.<leaf>'. Refusing to skip it: "
                "an unmatched pattern makes the lint pass by accident."
            )
        out.append(
            RuntimePopulatedPosition(
                section=str(entry["section"]),
                collection=parts[0],
                leaf=parts[2],
                populated_from=str(entry.get("populated_from", "")),
                reason=str(entry.get("reason", "")),
            )
        )
    return tuple(out)


def state_envelope_schema() -> dict[str, Any]:
    """The AG-UI state envelope schema (contract #4).

    Describes `{draftConfig, acceptance}` — the object a STATE_SNAPSHOT carries
    and a STATE_DELTA's pointers address. Deliberately separate from the config
    schema: that one describes the config *document*, this one the envelope it
    travels in, and conflating the two is the defect this contract exists to
    prevent (our pre-`7f88631` emitter was config-rooted and its deltas could
    not be applied to our own snapshot).
    """
    settings = get_settings()
    return _load_json(
        _resolve(settings.SKILL_BUILDER_STATE_ENVELOPE_PATH, "agui_state_envelope.json")
    )


def run_finished_schema() -> dict[str, Any]:
    """The `RUN_FINISHED.result` schema (contract #5).

    Ratified 2026-08-04 against this emitter, after we corrected four things in
    the gateway's second-hand draft. Its root is `additionalProperties: false`,
    so it is the one contract that can fail on a key we ADD: `AGUIEmitter.interrupt`
    forwards arbitrary `**detail`, and a new detail kwarg is now a hard rejection
    at the gateway rather than a field nobody reads.
    """
    settings = get_settings()
    return _load_json(
        _resolve(settings.SKILL_BUILDER_RUN_FINISHED_PATH, "agui_run_finished.json")
    )


def tool_schemas() -> dict[str, Any]:
    """The request_test_run / request_finalize tool-call schemas (contract #3)."""
    settings = get_settings()
    doc = _load_json(
        _resolve(settings.SKILL_BUILDER_TOOL_SCHEMAS_PATH, "tool_schemas.json")
    )
    tools = doc.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("tool-schemas contract must have an object under 'tools'")
    return tools
