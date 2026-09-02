---
type: Playbook
title: Deploy the skill-builder runtime
description: Build the ARM64 image, push it, create or update the AgentCore runtime — and the reasons the status tables in this repo should not be trusted.
resource: scripts/provision.py
tags: [deploy, agentcore, ecr, arm64]
timestamp: 2026-09-02
---

# Deploy the runtime

`scripts/provision.py` is idempotent and does the whole job: build and push the ARM64
image to ECR, then create or update the AgentCore runtime with
`serverProtocol: AGUI`. `--check` is a read-only inventory; a real deploy needs the
execution role.

## Things that will cost you an afternoon otherwise

**`--provenance=false` is required.** A provenance attestation makes the platform's image
resolution reject the artifact.

**ARM64 only.** The image is tagged by git short SHA, marked when the tree is dirty, and
pushed by digest.

**Update is a full replace**, so environment variables are always re-sent. Omitting one on
an update removes it.

**The ECR repository name is a permissions artefact, not a description.** It sits under a
namespace shared with a sibling runtime because that is what the account's policy allowed;
it does not mean the two are the same project.

## Do not trust the status tables in this repo

`README.md` and `CLAUDE.md` both carry a deployed-version table, and both explicitly warn
that it goes stale — one sat nine versions behind live. **Run `provision.py --check`** and
believe that instead. This bundle deliberately does not restate a version number for the
same reason.

## Model access

`scripts/check_model_access.py` reports whether the account's Bedrock Marketplace
agreement covers the model. A missing agreement fails at the model call, not at deploy, so
check it before concluding the runtime is broken —
[integrations/bedrock-claude](/integrations/bedrock-claude.md).

## Smoke test

`scripts/smoke_invoke.py` invokes the deployed runtime once and validates the stream's
well-formedness. The kickoff path calls no model and costs nothing; the continuation path
does call the model.
