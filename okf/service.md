---
type: Service
title: aeo-skill-builder-runtime
description: The Conversational Skill Builder — an AgentCore AGUI runtime that turns an org's onboarding context into a prospect-scanning skill config document through a streaming chat, one section at a time.
resource: arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_skill_builder-MQ0z2m8tqB
tags: [agentcore, agui, skill-builder, streaming, arm64]
timestamp: 2026-09-02
---

# aeo-skill-builder-runtime

The **Conversational Skill Builder**. An operator talks to it; it produces a
[skill config document](/business/skill-config-document.md) that
`configurable-prospect-scanner` can then run. It works through the config one section at
a time — geography, discovery, validation, contacts, scoring — proposing, then waiting for
a human to accept.

## The shape of it

| | |
|---|---|
| Runtime | AWS Bedrock AgentCore Runtime, `serverProtocol: AGUI`, ARM64, Python 3.12 |
| Wire | `POST /invocations` takes a run-input JSON and answers with an AG-UI **SSE event stream**; `GET /ping` is the liveness probe |
| Invoked by | the gateway, [aeo-backend](aeo-backend:/service.md), which relays the stream onward to the operator |
| Served on | port 8080, single uvicorn worker — the blocking model call is threadpooled and concurrency is capped gateway-side |
| Entry point | `app/skill_builder/server.py` is the ASGI app; [lib/runtime](/lib/runtime.md) holds the actual turn logic |

## The two properties everything else follows from

**It is emit-only.** No database handle, no queue, no write authority of any kind. When a
test run or a finalize needs to happen, this runtime **requests** it as a tool call and
the gateway performs it. See [business/emit-only](/business/emit-only.md) — it is the
boundary that makes an agent-driven authoring loop safe to run against real tenants.

**Each turn is stateless.** State arrives in the run input and leaves in the event
stream; nothing is retained between turns. Session isolation is a header the platform
supplies. That is what lets a turn be retried, and what keeps two operators' sessions
from touching.

## Where to go next

- One turn, end to end: [lib/runtime](/lib/runtime.md)
- What goes over the wire: [lib/agui-protocol](/lib/agui-protocol.md)
- The document being authored: [business/skill-config-document](/business/skill-config-document.md)
- Why the five pinned contracts exist: [lib/contracts](/lib/contracts.md)
- Ship it: [playbooks/deploy-runtime](/playbooks/deploy-runtime.md)
- What must be true: [capabilities/](/capabilities/index.md), proven by [qa/](/qa/index.md)
