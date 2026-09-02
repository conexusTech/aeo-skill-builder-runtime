---
type: Service Module
title: Tool calls — request and result
description: Emits request_test_run and request_finalize only after they validate, and parses the gateway's result back into the next turn.
resource: app/skill_builder/tools.py
tags: [tools, agui, gateway]
timestamp: 2026-09-02
---

# Tool calls

The two things this runtime can ask the gateway to do: **run a test scan**, and
**finalize** the config into a real skill. It cannot do either itself — see
[business/emit-only](/business/emit-only.md).

## Validate, then emit — or block

A tool call is validated before it is emitted, and blocked if it fails. Emitting an
invalid one would spend a real test run, or write a real skill, on a document already
known to be wrong; blocking it keeps the correction inside the conversation where the
operator can answer it.

## Results come back as three outcomes

A gateway tool result reports **succeeded**, **rejected**, or **declined**, with a summary
and structured issues, each classified as a schema violation, an org-coupling problem, or
other. Those three are different conversations: rejected means fix the document, declined
means a human said no, and succeeded means carry on. Collapsing them into
success-or-failure would lose the distinction between "this is wrong" and "not now".

The turn that follows a result is the after-tool mode in [lib/runtime](/lib/runtime.md).
