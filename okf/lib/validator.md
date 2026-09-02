---
type: Service Module
title: Validation and the two authoring lints
description: JSON-Schema validation of the config, tool arguments and result envelope, plus the org-coupling and unfilled-placeholder lints that stop a config that would only work for one org.
resource: app/skill_builder/validator.py
tags: [validation, jsonschema, lint, org-coupling]
timestamp: 2026-09-02
---

# Validation and the two authoring lints

Schema validation runs in two modes: **incremental** while a section is being drafted, and
**complete** when the config is about to be finalized. A partial draft failing "required"
checks mid-conversation is not an error; the same config failing them at finalize is.

## The two lints, and what they are really protecting

**Org coupling.** A skill config is authored once and run for many orgs. If the model
writes a literal — this town, this competitor, this job title — where a reference to the
org's context belongs, the config silently becomes single-tenant. It still validates. It
still runs. It just quietly produces one org's answer for everybody. The lint enforces a
reference at each position where the org must supply the value.

**Unfilled placeholders.** A leftover placeholder, or a context reference written inline
where a literal was required, fails the same way: valid document, wrong behaviour.

Both lints are **second implementations** of checks the gateway also runs. That
duplication is deliberate — this runtime blocks a bad tool call before emitting it, so the
operator gets the correction inside the conversation rather than as a rejection after a
round trip. The gateway's copy is the authority; see [lib/contracts](/lib/contracts.md)
for the drift test that keeps them honest.
