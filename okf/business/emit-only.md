---
type: Business Concept
title: Emit-only — the authority boundary
description: This runtime has no database handle and no write authority; it requests side effects and the gateway performs them.
resource: app/config.py
tags: [authority, safety, boundary, agent]
timestamp: 2026-09-02
---

# Emit-only

**This runtime cannot change anything.** No database handle, no queue client, no write
credential. It emits AG-UI events and tool-call *requests*; the gateway executes every
side effect — starting a test run, finalizing a skill, connecting to an existing one,
writing the session row.

## Why the boundary is here and not inside the agent

An authoring agent talking to real tenants will, eventually, propose something wrong. The
question is only what a wrong proposal can reach. Here it reaches a gateway that validates
independently and can **reject or decline** it — and the three-way result vocabulary in
[lib/tools](/lib/tools.md) exists because "the document is wrong" and "a human said no"
need different answers.

Putting the guard in the agent instead would mean trusting the component whose behaviour is
least predictable to be the one enforcing the limit. The lints in
[lib/validator](/lib/validator.md) do run here, but as a **courtesy that catches problems
early inside the conversation** — not as the authority. The gateway's copies are the
authority, which is what the drift test in [lib/contracts](/lib/contracts.md) protects.

This is also why each turn is stateless and why there is no scheduler, cron or background
worker in this repo at all: there is nothing here to keep.
