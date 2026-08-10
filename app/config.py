"""Settings for the Conversational Skill Builder runtime.

This is a **deliberately tiny** config. In `aeo-agent-service` these seven fields
lived at the bottom of a ~360-line `Settings` class alongside PostgreSQL, Redis,
Neo4j, Apify and the whole SoV surface — none of which this runtime touches. The
split into this repo is what lets the class contain only what the runtime reads.

⚠️ **`DATABASE_URL` and friends are gone on purpose.** This runtime is **emit-only**:
it emits AG-UI events and tool-call *requests*, and the gateway performs every
side-effect. It holds no database handle and no network write authority at all,
which is also the prompt-injection backstop. If a future change here wants a
connection string, that is the signal to stop and re-read the PRD, not to add a field.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Model -------------------------------------------------------------
    # Model choice (Q6, ours to pick): default to Claude Sonnet 5 on Bedrock — a
    # live, high-turn (~40) chat with a ~200k token budget billed per invocation,
    # so cost/latency matter and Sonnet 5 gives near-Opus agentic quality. Flip to
    # `anthropic.claude-opus-4-8` (the drop-in upgrade lever) if config-quality
    # evals show gaps. Hallucinated-config risk is backstopped by the gateway's
    # 3-point schema validation + mandatory test run + SU activation gate, so
    # Sonnet 5 is the safe default.
    #
    # 🔴 The bare `anthropic.` prefix is CORRECT — do not "normalise" it to
    # `us.anthropic.…`. Prefix forms are per-endpoint: we call Claude through the
    # Bedrock **Mantle** client, which wants the bare form and 404s on the other,
    # while `bedrock-runtime` wants exactly the opposite.
    SKILL_BUILDER_MODEL_ID: str = "anthropic.claude-sonnet-5"
    SKILL_BUILDER_AWS_REGION: str = "us-east-1"

    #: The build serving a turn, emitted on RUN_STARTED as `runtimeVersion`.
    #:
    #: Set by `scripts/provision.py` at deploy time from the image tag (the git
    #: SHA), so it identifies code rather than an AgentCore version number. It
    #: exists because a builder session pins to a warm container and keeps
    #: running the image it started on across deploys, with no signal anywhere —
    #: `get-agent-runtime` reports the CONFIGURED version, not the one serving a
    #: live session, which cost three false "reproductions" of a fixed defect.
    #:
    #: Empty means unstamped and the field is omitted from the wire, which is
    #: deliberately distinguishable from a stamp reading "unknown".
    SKILL_BUILDER_BUILD_VERSION: str = ""

    # --- Gateway-owned contracts (PRD §14) ---------------------------------
    # All FIVE are ratified v1, and the files bundled in app/skill_builder/stubs
    # are pinned verbatim copies — so empty here means "use the pinned copy", not
    # "use a guess". Set one to an absolute path to load the gateway's file
    # directly, which is how a conformance run detects a contract bump before it
    # reaches the wire.
    SKILL_BUILDER_CONFIG_SCHEMA_PATH: str = ""
    SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH: str = ""
    SKILL_BUILDER_TOOL_SCHEMAS_PATH: str = ""
    # Contract #4 (agui-state-envelope.json) — the {draftConfig, acceptance}
    # envelope that STATE_DELTA pointers address. Published 2026-08-03 as a direct
    # result of our own root-mismatch defect; it is the gateway's because R2's pipe
    # is what applies these events.
    SKILL_BUILDER_STATE_ENVELOPE_PATH: str = ""
    # Contract #5 (agui-run-finished.json) — `RUN_FINISHED.result`: the interrupt
    # vocabulary consumers render controls from, plus #14's `usage`. Its root is
    # CLOSED, which inverts how it breaks: the other four fail when the gateway
    # changes something, #5 fails when WE add something.
    SKILL_BUILDER_RUN_FINISHED_PATH: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
