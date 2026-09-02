---
type: Service Module
title: AG-UI protocol
description: The one module this repo owns outright — inbound run input, outbound event models, the emitter, SSE encoding, and the interrupt vocabulary.
resource: app/skill_builder/protocol/agui.py
tags: [agui, sse, protocol, contract]
timestamp: 2026-09-02
---

# AG-UI protocol

The wire. Inbound run input, the outbound event models, the emitter that produces them,
SSE encoding, the interrupt-reason vocabulary, and token-usage accounting.

## The state envelope, and the one shape mistake it prevents

State is `{draftConfig, acceptance}`, and **`acceptance` is a sibling of `draftConfig`,
never nested inside it.** Acceptance is a fact about the conversation — which sections a
human has agreed to — not a property of the document. Nesting it would put a
conversational artefact inside the document that gets handed to the scanner.

State deltas are emitted as pointers rooted at the config or at a section's acceptance, so
the consumer patches rather than re-renders.

## The run-finished result is a closed shape

Its schema forbids unknown properties, deliberately. Its `outcome` is one of interrupt,
tool call, or finalized; on an interrupt it carries the reason the run stopped, and the
declared vocabulary is wider than what is actually emitted today — one reason is defined
and never sent. Token usage is **omitted rather than zeroed** when no model ran, so "the
model was not called" and "the model was called and cost nothing" stay distinguishable.

The schema is one of the five pinned contracts — see [lib/contracts](/lib/contracts.md).
