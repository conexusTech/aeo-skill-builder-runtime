---
okf_version: "0.1"
conqrse_siblings:
  - name: aeo-backend
    path: ../aeo-backend
    description: The gateway. Calls this runtime's turn endpoint, relays the stream to the operator's browser, executes every side effect this runtime only requests, and owns the five contracts this repo pins verbatim.
  - name: aeo-frontend
    path: ../aeo-frontend
    description: The Meriwether portal that renders the builder chat the operator sees. Streams through the gateway, never directly from here.
  - name: aeo-agent-service
    path: ../aeo-agent-service
    description: The Python worker this runtime was split out of on 2026-08-07.
  - name: configurable-prospect-scanner
    path: ../configurable-prospect-scanner
    description: Consumes the skill config document this runtime authors, as config on the org's runtime context.
---

# OKF Bundle — aeo-skill-builder-runtime

Open Knowledge Format bundle for this repo. Start at [service.md](/service.md).

## Sections

- [service.md](/service.md) — the repo as a concept; agent entry point
- [endpoints/](/endpoints/invocations.md) — the two HTTP surfaces
- [lib/](/lib/runtime.md) — turn orchestration, the protocol, the prompt, the draft, the lints
- [business/](/business/skill-config-document.md) — domain concepts this repo owns
- [integrations/](/integrations/bedrock-claude.md) — outward calls
- [playbooks/](/playbooks/deploy-runtime.md) — operational runbooks
- [briefs/](/briefs/index.md) — product ⇄ design, before the code
- [capabilities/](/capabilities/index.md) — what the system does, as scenarios. No status
- [qa/](/qa/index.md) — one checklist per capability; every requirement has a check
- [log.md](/log.md) — change history

Reserved: `index.md` and `log.md` are never concept documents.
