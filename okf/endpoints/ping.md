---
type: API Route
title: GET /ping
description: Liveness probe that deliberately avoids constructing the chat model, so a model misconfiguration cannot fail liveness.
resource: app/skill_builder/server.py
tags: [route, health]
timestamp: 2026-09-02
---

# `GET /ping`

The platform's liveness check.

**It does not touch the chat model.** Resolving the model would make liveness depend on
Bedrock configuration and on a Marketplace agreement being in place — so a model problem
would present as a dead container, and the platform would recycle a process that is
running perfectly well. The distinction between "this container is alive" and "this
container can currently reach a model" is worth keeping.
