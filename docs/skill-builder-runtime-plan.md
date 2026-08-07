# Skill Builder — AgentCore runtime deployment plan

**Status: planned, not built.** No skill-builder runtime exists yet. This documents how we
create one, and the decisions that have to be made before the first command runs.

**Standing instruction (Leo, 2026-08-03):** AgentCore / Bedrock work is ours to implement
*and deploy*. This is not a hand-off document. The runbook we inherit from is
`docs/RUNBOOK-agentcore-runtime.md` in the sibling repo `aeo-groundtruth-browser-runtime` —
read its *"eight things that will bite you"* rather than re-deriving them.

## Decision: its own execution role, always

**The skill-builder runtime gets a dedicated execution role. We do not reuse another
runtime's.** Ruled by Leo 2026-08-03, mirroring how `aeo_groundtruth_browser` was isolated.

The tempting shortcut was
`AgentCore-aeoskills-produ-ApplicationAgentAeoAvLead-x0F0vuIYLBj6`, which already carries
`bedrock:InvokeModel`. Rejected for two concrete reasons, not on principle:

1. That name is **CloudFormation-generated** — it belongs to the prospecting stack. A stack
   update can replace or narrow it underneath us, and the failure would surface as a
   runtime that suddenly can't invoke a model.
2. It over-grants: it also carries `bedrock-agentcore:*ConfigurationBundle*`, which the
   builder has no business holding.

`AmazonBedrockAgentCoreAEOGroundTruthBrowserRole` is **not** an option either — it
deliberately has no `bedrock:InvokeModel` (that runtime drives a page and calls no model),
which is exactly the permission the builder needs most.

### Naming is load-bearing, not cosmetic

AWS's own documented AgentCore policy scopes `iam:PassRole` to
`arn:aws:iam::*:role/AmazonBedrockAgentCore*` and its role-management statement to
`*BedrockAgentCore*` (recorded in the sibling repo's `provision.py`). **A role named
anything else requires a bespoke policy written and applied by an administrator.**

So the names are chosen to land inside grants that plausibly already exist:

| Resource | Name | Why this name |
|---|---|---|
| Execution role | `AmazonBedrockAgentCoreAEOSkillBuilderRole` | Inside AWS's `AmazonBedrockAgentCore*` PassRole + role-management scope |
| Inline policy | `AEOSkillBuilderRuntimePolicy` | Mirrors `AEOGroundTruthBrowserRuntimePolicy` |
| ECR repository | `aeo-skill-builder` | Matches the existing flat `aeo-skill-*` repos, which is likely what the attached `aeo-skill-deploy-policy` covers |
| Runtime | `aeo_skill_builder` | AgentCore runtime names allow `[a-z0-9_]`; mirrors `aeo_groundtruth_browser` |

Existing ECR namespaces observed in this account: `aeo-groundtruth/browser`,
`aeoskills/aeo_hello_world`, `aeoskills/aeo_av_lead_scanner`, `aeo-skill-shared`,
`aeo-skill-just-a-skill`, `aeo-skill-av-prospect-finder`.

### Permission policy — how it differs from the ground-truth role

The two runtimes need almost disjoint permissions, which is the clearest argument for
separate roles.

| | GT browser | Skill builder |
|---|---|---|
| `bedrock:InvokeModel` / `WithResponseStream` / `CountTokens` | ❌ deliberately absent | ✅ **required** — it is a chat agent |
| `bedrock-agentcore:*BrowserSession*`, `ConnectBrowserAutomationStream` | ✅ | ❌ never launches a browser |
| `bedrock-agentcore:GetWorkloadAccessToken*` | ✅ | ✅ |
| Secrets Manager (`brightdata-*`) | ✅ | ❌ |
| CloudWatch Logs write | ✅ | ✅ |

## Why we cannot pre-verify whether this is self-serve

`iam:SimulatePrincipalPolicy` is denied, and so is `iam:GetPolicy` / `GetPolicyVersion` —
so the documents of the five policies attached to `user/leo.lindo`
(`BedrockAgentCoreFullAccess`, `bedrock-dev`, `aeo-dev-policy`, `aeo-groundtruth-policy`,
`aeo-skill-deploy-policy`) cannot be read. Policy *names* are visible; contents are not.

**The only way to learn whether `iam:CreateRole` is permitted is to attempt it.** Attempt
it with the final intended name and policy, not a throwaway probe — `iam:DeleteRole` is
denied, so anything created is not removable by us. If it is denied, the error text is the
precise admin request.

## Phases

### Phase 0 — settle the marketplace question BEFORE building

⚠️ **A correct role may still not be sufficient, and this is the risk most likely to waste
the build.** Foundation-model invocation from `user/leo.lindo` is refused with *"not
authorized to perform the required AWS Marketplace actions
(`aws-marketplace:ViewSubscriptions`, `aws-marketplace:Subscribe`)"* — verified 3× per
model against `us.anthropic.claude-sonnet-5`, `us.anthropic.claude-opus-4-8` and
`us.anthropic.claude-sonnet-4-5-…-v1:0`.

If that subscription gate is **account-level and unestablished**, the runtime's execution
role fails identically and we would only discover it after building, pushing and deploying.
The `aeoskills` runtimes hold `bedrock:InvokeModel` and sit `READY`, but `READY` means
*deployed*, not *successfully invoking*.

Cheapest resolution: invoke an existing `aeoskills` runtime and see whether it reaches a
model. Outcome decides everything downstream:

- **It invokes** → the gate binds only our IAM *user*. A new role unblocks the ARN, and the
  marketplace grant is needed only for local verification. Proceed.
- **It cannot** → an account-level marketplace grant is a hard prerequisite. Stop and
  escalate; building first buys nothing.

### Phase 1 — the two AWS writes

```bash
aws iam create-role  --role-name AmazonBedrockAgentCoreAEOSkillBuilderRole \
                     --assume-role-policy-document file://docs/policy-skillbuilder-trust.json
aws iam put-role-policy --role-name AmazonBedrockAgentCoreAEOSkillBuilderRole \
                     --policy-name AEOSkillBuilderRuntimePolicy \
                     --policy-document file://docs/policy-skillbuilder-runtime.json
aws ecr create-repository --repository-name aeo-skill-builder --region us-east-1
```

Trust policy: principal `bedrock-agentcore.amazonaws.com`, action `sts:AssumeRole`,
conditioned on `aws:SourceAccount = 082585646836` so no other account can assume it (the
confused-deputy guard the GT role already uses).

**`us-east-1` only.** The `bedrock-agentcore:*` grant carries an `aws:RequestedRegion`
condition that denies every other region outright.

### Phase 2 — close the code gaps

1. 🔴 **Add `GET /ping`.** `app/skill_builder/server.py` exposes `POST /invocations` but no
   `/ping`. AgentCore requires it for health; without it the runtime deploys and fails
   health checks. Must return 200 without constructing the Bedrock client, or a
   misconfigured model turns a health probe into a 500.
2. **ARM64 Dockerfile.** Port 8080, entrypoint `app.skill_builder.server:app`. Note this
   image needs **none** of the browser/Chromium weight — the GT runtime's plan task 9.2
   was wrong for the same reason (the browser is remote); it is doubly irrelevant here
   since the builder never opens a page.
3. **`scripts/provision_skillbuilder.py`**, mirroring the sibling repo's `provision.py`:
   idempotent, updates in place, keeps the same ARN, accepts `--role-arn` so it needs no
   IAM permission of its own once the role exists.

Two traps to inherit rather than rediscover:

- `docker buildx` needs **`--provenance=false`**. The default OCI attestation makes the
  artifact a manifest *list*, which AgentCore rejects without ever mentioning attestations.
- `update-agent-runtime` is a **full replace**. Omitting `--environment-variables` wipes
  them.

### Phase 3 — deploy and hand off

Build → push to `aeo-skill-builder` → `create-agent-runtime` with `--protocol AGUI` →
smoke-invoke → hand **runtime ARN + qualifier** to aeo-backend.

That hand-off is the single thing gating **R2** (the streaming turn endpoint) and therefore
the whole chat loop and all 13 acceptance criteria. Their endpoint exists today and 501s
with `SKILL_BUILDER_TURN_NOT_READY`.

### Environment the runtime needs

`SKILL_BUILDER_MODEL_ID` (default `anthropic.claude-sonnet-5` — **correct as-is**; the
Mantle endpoint wants the bare `anthropic.` prefix and 404s on `us.anthropic.…`, which is
the opposite of what `bedrock-runtime` wants), `SKILL_BUILDER_AWS_REGION`, and the three
contract paths — which can now be left empty, since the bundled defaults are pinned
verbatim copies of the ratified contracts.

## Open decisions

1. **Where the container lives** — this repo (`server.py` already imports `app.config` and
   builds as-is, but the image carries Redis/SoV/scrape dependencies) versus a new sibling
   repo matching the GT precedent (minimal image, but `app/skill_builder` then lives in two
   places or must be packaged). Not yet decided.
2. **Whether Phase 0 runs first.** Recommended, per the risk above.
