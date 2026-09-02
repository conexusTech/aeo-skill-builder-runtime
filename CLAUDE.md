# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## 🔧 This repo is OURS — development, runtime, and deployment

**Standing instruction from Leo (2026-09-02): we handle the Conversational Skill Builder
ourselves — the code, the AgentCore runtime, and the deployment to Bedrock AgentCore.**

Do not route it through another team, do not describe it as blocked on AWS access, and do
not write a permissions request. That was the *previous* situation and it ended when the
caller policy for account `082585646836` landed; those credentials are configured locally.
This is the same posture as the sibling ground-truth browser runtime (standing instruction
2026-08-03) and for the same reason.

Three things still need an administrator, so flag them **up front** rather than stalling
mid-task:

1. **A new execution role** — `iam:CreateRole` / `PutRolePolicy` are denied. Reusing
   `AmazonBedrockAgentCoreAEOSkillBuilderRole` keeps a feature self-serve. The
   `AmazonBedrockAgentCore` prefix is not cosmetic: AWS's own documented policy scopes
   `iam:PassRole` to `role/AmazonBedrockAgentCore*`, so a differently-named role needs a
   bespoke policy an administrator has to write and review.
2. **Any region other than `us-east-1`** — the grant carries an `aws:RequestedRegion`
   condition that denies the rest outright.
3. **An ECR namespace or secret prefix outside `aeo-groundtruth/*` and `brightdata-*`** —
   both are resource-scoped.

⚠️ **`bedrock-agentcore` and `bedrock` are different services**, granted by different
policies. And **caller access is not runtime access**: we can invoke models locally while a
runtime's execution role cannot.

## What this service is

The **Conversational Skill Builder** — a Bedrock AgentCore Runtime
(`serverProtocol: AGUI`) that turns an org's onboarding context into a prospect-scanning
**skill config document** through a streaming chat, iterating with a human per phase until
each section is accepted.

**Emit-only.** It emits AG-UI events and tool-call *requests*; the **gateway** performs
every side effect (test run, finalize, connect). It has no database handle and no network
write authority at all, which is also the prompt-injection backstop — the chat reads
untrusted customer context, so the blast radius of a successful injection is a bad
*request* the gateway can reject.

**Stateless per turn.** State is reconstructed from the gateway-supplied
`MESSAGES_SNAPSHOT` + `STATE_SNAPSHOT` on every invocation. The gateway's
`skill_builder_sessions` row is the only durable truth. Session isolation comes from the
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header (= gateway builder-session id =
threadId).

It was split out of `aeo-agent-service` on 2026-08-07: that service is a Redis-queue worker
holding PostgreSQL, Neo4j and Redis handles; this is an ASGI app with its own entrypoint,
image and execution role. Keeping them together shipped an image carrying the whole SoV
surface for a runtime that imports none of it. The only coupling was
`app.config.get_settings`, so `app/config.py` here is a seven-field port — its docstring
records what was deliberately left behind.

## Commands

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest

.venv/Scripts/python -m pytest tests/ -q                              # 299 tests, no AWS needed
.venv/Scripts/python -m pytest tests/test_skill_builder_contracts.py -q  # the drift test
```

⚠️ **`poetry run pytest` is wrong here** — that is the sibling service's toolchain. This
repo is venv + pip.

Nothing in the suite touches AWS or a network: the model is injected as a FastAPI
dependency and tests supply a `FakeChatModel`.

## Deploy

```bash
python scripts/provision.py --check                # read-only inventory -- ALWAYS first
python scripts/provision.py --role-arn <role-arn>  # build, push, create/update
```

Idempotent, and an update **keeps the same runtime ARN** — which matters, because that ARN
is what the gateway holds for R2.

| | |
|---|---|
| Runtime ARN | `arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_skill_builder-MQ0z2m8tqB` |
| Deployed | **v33**, `READY`, `SKILL_BUILDER_BUILD_VERSION=d4a969b@2fb2b5c343bd` (verified against AWS 2026-09-02) |
| ECR | `aeo-groundtruth/skill-builder` — the "groundtruth" namespace is a permissions artifact, see `provision.py` |
| Execution role | `AmazonBedrockAgentCoreAEOSkillBuilderRole` |
| Model | `anthropic.claude-sonnet-5` on Bedrock (`SKILL_BUILDER_MODEL_ID`), Opus as a config lever |

⚠️ **Read the deployed version from `provision.py --check` and the runtime's own
`SKILL_BUILDER_BUILD_VERSION`, not from a table.** The README's status block was wrong for
longer than it was right — it carried "role blocked / runtime not created" while v23 was
live, and a reader reasoning from it concluded the runtime did not exist and went to AWS to
find out. Every version number in a markdown file here, including the one above, is a
snapshot.

`SKILL_BUILDER_BUILD_VERSION` is `<git-tag>@<digest-prefix>`. Both halves earn their place:
the tag says which source, the digest says which artifact, and they can legitimately
disagree (a `-dirty` tag, or a rebuild of one commit).

### Three deploy traps

Inherited from `aeo-groundtruth-browser-runtime/docs/RUNBOOK-agentcore-runtime.md`, worth
reading in full before touching AWS here.

1. **`docker buildx` needs `--provenance=false`.** The default OCI attestation makes the
   pushed artifact a manifest *list*, which AgentCore rejects without ever mentioning
   attestations.
2. **`update-agent-runtime` is a full REPLACE, not a merge.** Omitting
   `environmentVariables` wipes them, and the damage is invisible while the values match
   the defaults in `config.py`.
3. **`CreateAgentRuntime` authorizes three actions whose names are not inferable** — it
   implicitly creates a DEFAULT endpoint and a workload identity whose resource is not a
   `runtime/*` ARN. `provision.py` prints AWS's own error text rather than guessing which
   grant to request, because a grant on the wrong resource denies byte-for-byte
   identically.

## Layout

| Path | What |
|---|---|
| `app/skill_builder/server.py` | the ASGI app: `POST /invocations`, `GET /ping` |
| `app/skill_builder/runtime.py` | `handle_turn` — never crashes; failures become in-stream `RUN_ERROR` |
| `app/skill_builder/protocol/agui.py` | the one owned AG-UI module: events, emitter, SSE encoding |
| `app/skill_builder/prompt.py` | six-layer prompt composition + stable/volatile `split()` for the cache breakpoint |
| `app/skill_builder/validator.py` | jsonschema validation — incremental vs `require_complete` |
| `app/skill_builder/org_coupling.py` | the R12 org-coupling lint, mirroring the gateway's `org-coupling.lint.ts` |
| `app/skill_builder/authoring_placeholders.py` | the unfilled-authoring lint, mirroring `authoring-placeholders.lint.ts` |
| `app/skill_builder/contracts.py` + `stubs/` | the **five pinned contracts** and the `SKILL_BUILDER_*_PATH` swap points |
| `scripts/provision.py` | idempotent deploy — create or update in place, same ARN |

`app/skill_builder/README.md` is the module-level guide and stays authoritative for the
internals.

## The five pinned contracts, and the re-pin loop

`stubs/` holds **verbatim copies of five gateway-owned contract files**:

| Stub | Override env var |
|---|---|
| `config_schema.json` | `SKILL_BUILDER_CONFIG_SCHEMA_PATH` |
| `context_field_keys.json` | `SKILL_BUILDER_CONTEXT_FIELD_KEYS_PATH` |
| `tool_schemas.json` | `SKILL_BUILDER_TOOL_SCHEMAS_PATH` |
| `agui_state_envelope.json` | `SKILL_BUILDER_STATE_ENVELOPE_PATH` |
| `agui_run_finished.json` | `SKILL_BUILDER_RUN_FINISHED_PATH` |

They **cannot self-update**, so a gateway-side change means a re-pin here.
`tests/test_skill_builder_contracts.py` hashes ours against the gateway's real files at
`../../aeo-backend/src/backend/skills/config/`, resolved as
`Path(__file__).resolve().parents[2]` — which keeps working after the split only because
this repo sits at the same depth beside `aeo-backend` as the old one did. It **skips when
that directory is absent** and guards its own skip, so a path typo fails loudly rather than
leaving the test green while comparing nothing.

**The loop, in order** (twelve pins landed in one day, and skipping a step cost a
round trip each time):

1. Verify **all five** pinned files, not just the one the other side mentioned.
2. Re-pin until the drift test is green.
3. **Restart the local runtime** — `contracts.py` `@lru_cache`s the schema **per process**,
   so a re-pin without a restart validates against the previous contract.
4. Commit, then post the confirmation with timestamps.

Two rules that each cost a real detour:

- 🔴 **A deploy does not follow a pin automatically.** A deployed version went two pins
  stale within hours. **Run the drift test before any deploy**, not after.
- 🔴 **"No pinned file changed in this commit" is not "your pins are current."** The drift
  test went red from a gateway commit landing seven minutes after our own last re-pin.

## Things that will bite you

- **Never raise `_NOTE_BUDGET`** (400 chars, `prompt.py`). The gateway owner reorders their
  descriptions to fit it; raising the cap drags ~20 unrelated descriptions into the prompt.
  When a rule does not fit, ask for a reorder — a 380-char candidate was measured and it
  renders whole.
- **What renders is one level from a section property, following `$ref`.** `scoring.fit`
  renders; `tiers.description` does not, because it sits two levels down under
  `factors.items`. That is why a gateway correction written into `tiers.description` was
  discarded unread, and why 34 nested descriptions / 6,446 chars never reached the model —
  the four largest being the four things that broke.
- **Validate with `validator_for`, never a hardcoded draft class.** The validator was
  pinned to `Draft202012Validator` against contracts that all declare **draft-07**, and
  `dependencies` was *removed* in 2020-12 — so a gateway rule expressed with that keyword
  did not exist for us and was ignored in silence. Their ajv is draft-07, so the gateway
  gates held; what was lost was the in-session catch. **Choosing an undroppable mechanism
  does not help if you never check the mechanism you moved to.**
- **A keyword table is a recognizer, not a domain.** Substring matching proves it, so
  deriving a closed discovery enum from one inverts its intent.
- **Pipeline stages are static in code, forever.** No sixth authoring section, ever; the
  exclusion is pinned as a test.
- **Compressing a rule to survive truncation can cut the part that makes it actionable.**
  One rule was compressed to fit the note budget and lost the "keyword to points" clause
  that made it followable.
- **A test can assert a property the FAILING case also satisfies** — this is the single
  most repeated defect here, in four variants: asserting length; a set watching only for
  growth; presence of a *name* when the hint was a bare `value`; presence of a string
  outside the truncation boundary. Fix with a contract token, a structural key (`const` /
  `default`), or the **absence** of the degraded output — never the presence of a name.
- **The smoke path calls no model.** A green deploy proves the stream, not the prompt layer
  — whether the model *follows* a new rule is decided by the gateway's end-to-end run.

## 🛡️ CSB is a protected baseline — never regress it

Standing instruction after PO praise (2026-08-28): **every change must hold or improve
output quality.** Three traps make that harder here than it sounds:

1. **The test suite is the only gate** — there is no ruff or mypy in this venv.
2. **Behaviour can change with a zero-line Python diff**, because the contracts are data.
3. **A same-length description is not the same behaviour.**

## Related

- `aeo-agent-service` — the Redis worker this was split out of; its `CLAUDE.md` carries the
  platform-wide context (Phases 1.5 / 2 / 3) and an OKF bundle at `okf/`
- `aeo-groundtruth-browser-runtime` — the sibling AgentCore runtime and its runbook
- `aeo-backend` — the gateway; owns the five contracts, `skill_builder_sessions`, and every
  side effect this runtime requests
- `conqrse-projects/aeo-triage/conversational-skill-builder.md` — the cross-repo tracker and
  live thread (five repos ship parts of this feature)
