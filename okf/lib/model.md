---
type: Service Module
title: Chat model seam
description: One interface with two implementations — a fake used throughout the tests, and the production Claude-on-Bedrock adapter.
resource: app/skill_builder/model.py
tags: [model, bedrock, testing, seam]
timestamp: 2026-09-02
---

# Chat model seam

A single interface behind which sit a **fake** model and the **Bedrock** one. The fake is
what lets the entire suite — protocol, phases, prompt composition, lints, tool emission,
the server itself — run with no AWS credentials and no spend, injected by dependency
override.

The model returns a **structured decision**, not prose: which action to take, the message
to show, the phase and section it concerns, an optional matched slug and vertical, notes,
an interrupt reason, and token usage. The turn logic branches on that decision. Keeping it
structured is what stops turn orchestration from parsing free text to decide what happens
next.

Which model and which region are environment-configured. The vendor extra that supplies
the AWS transport is load-bearing — see [integrations/bedrock-claude](/integrations/bedrock-claude.md).
