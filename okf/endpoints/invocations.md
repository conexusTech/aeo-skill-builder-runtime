---
type: API Route
title: POST /invocations
description: One chat turn. Takes a run-input JSON and answers with an AG-UI SSE event stream, never a raw 500.
resource: app/skill_builder/server.py
tags: [route, agui, sse, streaming]
timestamp: 2026-09-02
---

# `POST /invocations`

The only real surface. Parses the run input, hands the turn to
[lib/runtime](/lib/runtime.md) off the event loop, and streams the resulting AG-UI events
back.

**It never raises a raw 500.** A malformed body and an internal failure both become a
run-error **inside the stream**. That matters because the consumer is already reading an
event stream by the time anything can go wrong: a transport-level error mid-stream is far
harder to render to an operator than an error event, and would leave the browser holding a
half-finished conversation with no explanation.

**The model call is moved off the event loop.** It blocks, and blocking the loop would
stall the stream this endpoint exists to produce.

**Session isolation comes from a platform header**, not from anything this runtime
tracks — consistent with each turn being stateless. See [service.md](/service.md).

Shapes: [lib/agui-protocol](/lib/agui-protocol.md).
