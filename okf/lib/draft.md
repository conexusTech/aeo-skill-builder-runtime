---
type: Service Module
title: Draft construction and state
description: The skeleton, the RFC-6902 patching, the single owner of a section write, slug derivation, and the phase ordering that decides what comes next.
resource: app/skill_builder/draft.py
tags: [draft, patch, state, phases]
timestamp: 2026-09-02
---

# Draft construction and state

Builds the config skeleton, computes and applies JSON patches, derives the slug, injects
seeded defaults, and owns **the one function that writes a section**.

That single-writer rule is the point. A section write has to move three things together —
the document, its acceptance flag, and the state delta the consumer receives. Three call
sites doing two of the three is how a UI ends up showing a section as accepted that the
document does not contain.

The companion state module holds the phase ordering (geography, discovery, validation,
contacts, scoring) and the completion helpers that answer "what should this turn work on?"
The conversation advances one section at a time because a human accepts one section at a
time.

The untrusted onboarding context is read defensively — every field treated as absent or
malformed until proven otherwise, and fenced before it reaches
[lib/prompt](/lib/prompt.md).
