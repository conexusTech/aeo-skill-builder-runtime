---
type: Service Module
title: The five pinned contracts
description: Verbatim copies of the gateway's five schemas, loaded through one resolver, each overridable by path, and guarded by a cross-repo drift test.
resource: app/skill_builder/contracts.py
tags: [contract, cross-repo, drift, schema]
timestamp: 2026-09-02
---

# The five pinned contracts

This runtime and the gateway must agree on five shapes: the **config schema**, the
**context-field keys and their bindings**, the **tool schemas**, the **AG-UI state
envelope**, and the **run-finished result**. This repo carries a verbatim copy of each,
loaded through one cached resolver, and each can be overridden by an environment path so a
deploy can be pointed at a newer copy without a rebuild.

## Why copies rather than a shared package

There is no published package between a Python runtime and a TypeScript gateway, so the
alternative to a copy is a hand-written second description — which drifts without anything
noticing. A verbatim copy at least *can* be compared.

## The drift test is the actual safeguard

A test hashes the five bundled copies against the gateway's originals in the sibling
checkout, and **skips loudly when that sibling is absent** rather than passing quietly.
That distinction is the whole value: a silent skip on a developer machine without the
sibling checked out would make the contract look verified everywhere while being verified
nowhere.

The context-field keys are a **closed vocabulary**, with a set of enforced position
bindings and a set of positions the config must never author because the runtime populates
them. That is the data behind the org-coupling lint in [lib/validator](/lib/validator.md).
