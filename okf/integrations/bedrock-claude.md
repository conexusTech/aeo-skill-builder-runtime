---
type: External Integration
title: Claude on Amazon Bedrock
description: The per-turn structured decision — which action to take next — comes from Claude via Bedrock, lazily constructed and never touched by the health check.
resource: https://aws.amazon.com/bedrock/
tags: [bedrock, claude, model, aws]
timestamp: 2026-09-02
---

# Claude on Amazon Bedrock

The one model call. Per turn, it returns the structured decision described in
[lib/model](/lib/model.md) — propose a section, request a test run, finalize, connect, or
wait for a human.

**Configuration** is two environment variables: the model id and the region, both with
defaults. The build version is a third, stamped at provision time so a running container
can say which image it is.

**The client is constructed lazily**, and [endpoints/ping](/endpoints/ping.md) deliberately
does not construct it — a model misconfiguration should not read as a dead container.

**The vendor SDK's AWS extra is load-bearing**, not cosmetic: it is what pulls in the AWS
transport the Bedrock client needs. Removing it to "slim the image" breaks the model call
at runtime rather than at install time.

Access also depends on a **Bedrock Marketplace agreement** being in place for the model on
the account, which is account state rather than code — there is a script that reports it,
named in [playbooks/deploy-runtime](/playbooks/deploy-runtime.md).
