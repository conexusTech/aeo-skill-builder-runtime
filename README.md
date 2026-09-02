# aeo-skill-builder-runtime

The **Conversational Skill Builder** — a Bedrock AgentCore Runtime (`serverProtocol:
AGUI`) that turns an org's onboarding context into a prospect-scanning **skill config
document** through a streaming chat.

**Emit-only.** It emits AG-UI events and tool-call *requests*; the gateway performs
every side-effect. It has no database handle and no network write authority at all,
which is also the prompt-injection backstop.

Stateless per turn — state is reconstructed from the gateway's snapshots on every
invocation. `skill_builder_sessions` (gateway-owned) is the only durable truth.

## Ownership

**We own this end to end — development, the runtime, and deployment to Bedrock AgentCore**
(standing instruction from Leo, 2026-09-02). It is not another team's and it is not blocked
on AWS access. See [CLAUDE.md](CLAUDE.md) for the self-serve boundary and the three things
that still need an administrator.

## Why this repo exists

Split out of `aeo-agent-service` on 2026-08-07, mirroring how
`aeo-groundtruth-browser-runtime` was separated. That service is a Redis-queue worker;
this is an ASGI app with its own entrypoint, its own image and its own execution role.
Keeping them together meant an image carrying PostgreSQL, Redis, Neo4j and the whole
SoV surface for a runtime that imports none of it.

**`app/skill_builder/` moved here verbatim.** The only coupling to the old service was
`app.config.get_settings`, so `app/config.py` here is a seven-field port — see its
docstring for what was deliberately left behind.

## Layout

| Path | What |
|---|---|
| `app/skill_builder/server.py` | the ASGI app: `POST /invocations`, `GET /ping` |
| `app/skill_builder/runtime.py` | `handle_turn` — never crashes; failures become in-stream `RUN_ERROR` |
| `app/skill_builder/protocol/agui.py` | the one owned AG-UI module: events, emitter, SSE encoding |
| `app/skill_builder/stubs/` | the **five pinned contracts**, verbatim copies of the gateway's ratified files — `config_schema`, `context_field_keys`, `tool_schemas`, `agui_state_envelope`, `agui_run_finished` |
| `scripts/provision.py` | idempotent deploy — create or update in place, same ARN |
| `docs/admin-request-skillbuilder-role.md` | the one thing we cannot self-serve |

`app/skill_builder/README.md` is the module-level guide and stays authoritative for
the internals.

## Develop

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest tests/ -q          # 299 tests, no AWS needed
```

Nothing in the suite touches AWS or a network: the model is injected as a FastAPI
dependency and tests supply a `FakeChatModel`.

⚠️ **`tests/test_skill_builder_contracts.py` hashes the five pinned stubs against the
gateway's real files** at `../../aeo-backend/src/backend/skills/config/`, resolved as
`Path(__file__).resolve().parents[2]`. That happens to keep working after the move
only because this repo sits at the same depth beside `aeo-backend` as the old one did.
It **skips when that directory is absent** and guards its own skip, so a path typo
fails loudly rather than leaving the test green while comparing nothing.

## Deploy

```bash
python scripts/provision.py --check                # read-only inventory
python scripts/provision.py --role-arn <role-arn>  # build, push, create/update
```

Idempotent, and an update **keeps the same runtime ARN** — which matters, because that
ARN is what the gateway holds for R2.

### Three things that will bite you

Inherited from the sibling repo's `docs/RUNBOOK-agentcore-runtime.md`, which is worth
reading in full before touching AWS here.

1. **`docker buildx` needs `--provenance=false`.** The default OCI attestation makes
   the pushed artifact a manifest *list*, which AgentCore rejects without ever
   mentioning attestations.
2. **`update-agent-runtime` is a full REPLACE, not a merge.** Omitting
   `environmentVariables` wipes them, and the damage is invisible while the values
   match the defaults in `config.py`.
3. **`CreateAgentRuntime` authorizes three actions whose names are not inferable** —
   it implicitly creates a DEFAULT endpoint and a workload identity whose resource is
   not a `runtime/*` ARN. `provision.py` prints AWS's own error text rather than
   guessing which grant to request, because a grant on the wrong resource denies
   byte-for-byte identically.

## Status

Verified against AWS on 2026-09-02, not transcribed.

| | |
|---|---|
| Code | ✅ 299 tests pass |
| ECR repository | ✅ `aeo-groundtruth/skill-builder` (see the namespace note in `provision.py`) |
| Execution role | ✅ `AmazonBedrockAgentCoreAEOSkillBuilderRole` |
| Runtime / ARN | ✅ `arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_skill_builder-MQ0z2m8tqB` |
| Deployed | ✅ **v33**, `READY` — `SKILL_BUILDER_BUILD_VERSION=d4a969b@2fb2b5c343bd` |
| Bedrock model access | ✅ established — model-backed turns run (`anthropic.claude-sonnet-5`) |

This block sat at **v24** while v33 was live, which is the same failure it warns about
below — nine versions of drift in a table nobody re-verified.

⚠️ **This table was wrong for longer than it was right, and it cost a real detour.** It
carried "role blocked / runtime not created" through v23 being live, so a reader
reasoning from it concluded the runtime did not exist and went to AWS to find out.
**Check `provision.py --check` before trusting this block** — it is read-only, takes a
second, and is the only statement here that cannot go stale.

Deploys are recorded by `SKILL_BUILDER_BUILD_VERSION` on the runtime itself, in the form
`<git-tag>@<digest-prefix>`. Both halves earn their place: the tag says which source, the
digest says which artifact, and they can legitimately disagree (a `-dirty` tag, or a
rebuild of one commit). That value is the answer to "what is actually running", and it is
authoritative where this file is not.

## Related

- `aeo-agent-service` — the Redis worker this was split out of
- `aeo-groundtruth-browser-runtime` — the sibling AgentCore runtime and its runbook
- `conqrse-projects/aeo-triage/conversational-skill-builder.md` — the cross-repo
  tracker and Live thread (five repos ship parts of this feature)
