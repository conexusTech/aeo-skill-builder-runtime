---
type: Service Module
title: Prompt composition
description: A seven-layer prompt split into a stable and a volatile half so the stable half sits behind a cache breakpoint.
resource: app/skill_builder/prompt.py
tags: [prompt, cache, bedrock]
timestamp: 2026-09-02
---

# Prompt composition

Seven layers, composed per turn, then **split into a stable prefix and a volatile
suffix**. The split exists so the stable half can sit behind a prompt-cache breakpoint:
the instructions, the schema-derived section shapes and the vocabulary do not change
between turns, while the conversation and the current draft do.

Section shapes are **rendered from the config schema**, not written out again in prose.
That means adding a field to the schema changes what the model is told, in one place, and
a hand-maintained second description cannot drift from the thing being validated against.

The untrusted onboarding blob reaches the prompt through the defensive reader in
[lib/draft](/lib/draft.md)'s companion context module, fenced as a quoted block rather
than spliced into instructions — an org's onboarding text is user input, and it arrives
from outside.
