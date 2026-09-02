---
type: External Integration
title: The gateway (aeo-backend)
description: The only caller, and the only thing that can act on what this runtime proposes.
resource: aeo-backend:/service.md
tags: [gateway, agui, cross-repo]
timestamp: 2026-09-02
---

# The gateway

Inverted, as integrations go: **the gateway calls this runtime**, not the other way round.
Nothing here holds a client for it.

It does three things this repo depends on:

- **Relays the stream.** It calls the turn endpoint and passes the AG-UI events on to the
  operator's browser, so the chat renders progressively.
- **Executes the side effects.** Test runs, finalize, connect, and the session row. See
  [business/emit-only](/business/emit-only.md).
- **Owns the five contracts.** The copies in this repo are pinned verbatim from it, and a
  drift test compares them — [lib/contracts](/lib/contracts.md).

It also **caps concurrency**, which is why this container runs a single worker.

Authentication and authorization are the platform's, at the AgentCore and IAM layer,
outside this repo. Session isolation arrives as a platform header.
