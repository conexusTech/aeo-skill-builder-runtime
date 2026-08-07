"""Bundled copies of the four gateway-owned contracts (PRD §14/§15).

  1. skill-builder-config.schema.json  → config_schema.json
  2. the well-known context-field keys → context_field_keys.json
  3. request_test_run / request_finalize tool schemas → tool_schemas.json
  4. agui-state-envelope.json          → agui_state_envelope.json

**These are no longer stubs.** All four were published and RATIFIED as v1 on
2026-08-03, and the files here are **pinned verbatim copies** of them (their
titles say `RATIFIED` and they carry `version: 1.0`). They were hand-written
guesses before that, and a guess that contradicts a ratified contract is worse
than no bundled copy at all: it makes "schema-valid" mean valid against a
fiction, which is exactly how several defects survived.

Contract 4 is the one *we* caused: the envelope existed only in a cross-repo
thread until our config-rooted STATE_DELTA proved three repos held mutually
incompatible views of it, so the gateway published it as a file.

⚠️ Because the copies are pinned, they cannot self-update — a `version` bump on
the gateway side has to be communicated and re-copied deliberately. Canonical
source: `src/backend/skills/config/` in `aeo-backend`. `tests/
test_skill_builder_contracts.py` asserts the pins are byte-identical to that
directory when it is present, so a drift is a test failure rather than a
discovery months later; a doc-only drift on the config schema went unnoticed
between 2026-08-03 and 2026-08-04 for exactly that reason.

`app.skill_builder.contracts` loads these, or a gateway-supplied override path
(`SKILL_BUILDER_*_PATH`) — the single swap point.
"""
