# Skill Builder Agent (Bedrock AgentCore AGUI runtime)

A **third product surface** for `aeo-agent-service`, architecturally distinct
from the Redis-worker app in `app/main.py`. It is a conversational config-author:
a streaming AG-UI chat that turns an org's onboarding context into a
prospect-scanning **skill config document**, iterating with a human per phase
until each section is accepted.

> **Emit-only.** The agent emits events + tool-call requests; the **gateway**
> performs all persistence and every side-effect (test run, finalize, connect).
> The agent has no direct DB or network write authority — also a
> prompt-injection backstop. It never scans, never writes to Postgres/Neo4j,
> never calls conqrse-queue.

## Deploy

`app/skill_builder/server.py:app` is the ASGI server deployed to **Bedrock
AgentCore Runtime with `--protocol AGUI`**. It serves the documented AGUI
contract: `POST /invocations` (RunAgentInput in → AG-UI SSE out), with session
isolation via the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header
(= gateway builder-session id = threadId). Each turn is stateless and fully
reconstructed from the gateway-supplied `MESSAGES_SNAPSHOT` + `STATE_SNAPSHOT`;
the gateway's `skill_builder_sessions` row is the sole durable source of truth.

**Deploy-time dependency:** `anthropic[bedrock]` (the Bedrock Mantle client,
lazily imported in `model.py`). Not required to import the package or run tests.

## Module map

| File | Role |
|------|------|
| `protocol/agui.py` | The one owned AG-UI module — inbound `RunAgentInput`; outbound `TEXT_MESSAGE_*`, `STATE_DELTA`, `STATE_SNAPSHOT`, `RUN_STARTED/FINISHED`(+interrupt), `RUN_ERROR`, `TOOL_CALL_*`; `AGUIEmitter`; SSE encoder. |
| `state.py` | `BuilderState` (`draftConfig` + `acceptance`), phase order. |
| `context.py` | Customer-context loader + injection guardrail (free-text fenced as data). |
| `prompt.py` | Six-layer prompt composition + stable/volatile `split()` for the cache breakpoint. Layer 4 carries the R12 `context_ref` syntax + the closed key vocabulary (sorted — the layer is inside the cached prefix, and `frozenset` order varies per process). |
| `draft.py` | Schema-valid draft skeleton + RFC-6902 `STATE_DELTA` generation. |
| `validator.py` | jsonschema validation — incremental vs `require_complete`. |
| `catalog.py` | Library-first match (vertical + lead type + skill type, active only). |
| `tools.py` | `request_test_run` / `request_finalize` emit + gateway-result / rejection handling. |
| `model.py` | The `ChatModel` seam — `FakeChatModel` (tests) + `BedrockChatModel` (Sonnet 5, Opus 4.8 lever). |
| `runtime.py` | Stateless turn handler tying it together; never crashes (→ in-stream `RUN_ERROR`). |
| `server.py` | The AgentCore AGUI ASGI entry — `POST /invocations` + `GET /ping`. |
| `stubs/` + `contracts.py` | Pinned copies of the three gateway-owned contracts + the single `SKILL_BUILDER_*_PATH` swap point. |

## Contracts (PRD §14) — RATIFIED v1, 2026-08-03

All three gateway-owned contracts are **published and ratified**, and the files in
`stubs/` are **pinned verbatim copies** of them (they were hand-written guesses
before). Overriding via `SKILL_BUILDER_CONFIG_SCHEMA_PATH` /
`_CONTEXT_FIELD_KEYS_PATH` / `_TOOL_SCHEMAS_PATH` still works and needs no code
change. ⚠️ Because the copies are pinned, a `version` bump on the gateway side has
to be communicated — they cannot self-update.

Confirmed against the ratified contract: what `runtime._kickoff` emits validates
with **zero issues** incrementally, and a fully-decided config with zero in both
modes. `type` is the one field absent mid-conversation — the chat decides it, and
both tool emitters validate `require_complete` before emitting, so no tool call
escapes without it.

Shapes pinned with the gateway and frontend rather than guessed:

- **`TOOL_CALL_*`** — `START {type,toolCallId,toolCallName,parentMessageId?}` →
  `ARGS {type,toolCallId,delta}` → `END {type,toolCallId}`. `delta` is the
  **complete** args object as one JSON string, not token-streamed. Results never
  arrive on `END`.
- **`STATE_DELTA` is rooted at the state envelope**, not the config:
  `/draftConfig/<section>/…`, with `/acceptance/<section>` as a **sibling**
  (never nested — conversation state must not reach `skills.config`). Re-rooting
  happens in `AGUIEmitter.state_delta`; callers keep computing config-relative
  patches. An un-rooted patch cannot be applied to the envelope we ourselves
  snapshot.
- **Config keys are snake_case verbatim** (`product_description`, `lead_type`,
  `run_parameters`).
- **Org-specific values bind as `{"context_ref": "<key>"}` objects**, optionally
  with a `default` sibling — never a bare literal, never a dotted string, and
  never prefixed. The vocabulary is closed; an unknown key is a hard R12 failure.
- **Catalog** arrives on `forwardedProps.catalog`; its three R13 match dimensions
  read one vocabulary — `vertical`, `lead_type` (`A`/`B`/`MIXED`) and `skill_type`
  (`skills.type`: `customer`/`project`).

**We still owe the gateway the runtime ARN + qualifier** — the one thing gating R2
and therefore the whole chat loop.

## Model choice (Q6)

Default `anthropic.claude-sonnet-5` on Bedrock (`SKILL_BUILDER_MODEL_ID`), with
`anthropic.claude-opus-4-8` as a config-flag lever. Adaptive thinking; a manual
`cache_control` breakpoint on the stable prompt prefix (Bedrock has no automatic
caching and turns re-send history — the main cost lever, §13).

## Launch posture

Config-only. Custom-module generation (R11) is OFF at launch — every skill is
expressible in the standard phase vocabulary.

## Testing

Fully testable against a mocked stream with no AWS/model dependency: tests
assert over `AGUIEmitter.events` and inject `FakeChatModel`. `poetry run pytest
tests/test_skill_builder_*.py`.
