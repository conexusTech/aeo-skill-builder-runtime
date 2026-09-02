---
type: Service Module
title: Turn orchestration
description: handle_turn — the stateless per-turn logic behind the invocation endpoint, in four modes, which never crashes.
resource: app/skill_builder/runtime.py
tags: [turn, orchestration, agui]
timestamp: 2026-09-02
---

# Turn orchestration

`handle_turn` is the whole turn. It is **stateless**: everything it needs arrives in the
run input, and everything it produces leaves as events.

## Four modes

| Mode | When |
|---|---|
| Kickoff | the first turn of a new build |
| Edit kickoff | the first turn of a session editing an existing config |
| Continue | an ordinary turn mid-conversation |
| After tool | the turn following a gateway tool result |

The "after tool" mode is what makes the loop work at all: this runtime asked for a test
run or a finalize, the gateway did it, and the outcome comes back as input to the next
turn. See [lib/tools](/lib/tools.md).

## It converts failure into an event rather than an exception

Every failure path ends in a run-error event, never a raised exception escaping to the
transport. The reason is in [endpoints/invocations](/endpoints/invocations.md): the
consumer is mid-stream, and an error it can render beats a broken connection.

## Section writes go through one door

A turn may propose a section, request a tool call, finalize, connect to a matched existing
skill, or wait for a human. When it writes a section it does so through the single writer
in [lib/draft](/lib/draft.md) — one owner for section writes, so the patch, the acceptance
flag and the emitted state delta cannot disagree.
