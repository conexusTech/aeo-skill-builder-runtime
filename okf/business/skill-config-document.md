---
type: Business Concept
title: The skill config document
description: What this runtime produces — a config authored once and run for many orgs, which is why references matter more than values.
resource: app/skill_builder/stubs/config_schema.json
tags: [config, skill, authoring]
timestamp: 2026-09-02
---

# The skill config document

The deliverable. A JSON document describing how to find and score prospects for one kind
of business, which [configurable-prospect-scanner](configurable-prospect-scanner:/service.md)
then executes.

Beyond identity fields — version, name, slug, type, product description, vertical, lead
type and run parameters — it carries the **five authoring sections** the conversation works
through in order: geography, discovery, validation, contacts, scoring.

## One document, many orgs

This is the property that shapes everything else. A config is authored once for a
*vertical* and run for *every org* in it. So wherever a value belongs to the org rather
than to the vertical, the document must hold a **reference** to the org's context, not a
literal — and the closed vocabulary of context keys is what those references are drawn
from.

Get that wrong and the document is still valid and still runs. It simply produces one
org's answer for everyone, silently. That is why the org-coupling lint exists in
[lib/validator](/lib/validator.md), and why it runs here as well as in the gateway.

## What the agent must not author

Some positions are populated by the runtime at scan time. The config declaring them would
overwrite work the scanner does for itself, so the contract names them explicitly as
off-limits. The execution-phase list is the same: runtime-owned, never agent-authored.
