---
type: Service Module
title: Library-first matching
description: Matches vertical, lead type and skill type against the gateway-supplied catalog of active skills, so an existing skill is connected rather than a near-duplicate authored.
resource: app/skill_builder/catalog.py
tags: [catalog, matching, reuse]
timestamp: 2026-09-02
---

# Library-first matching

Before authoring, check whether the thing already exists. The runtime matches the
requested vertical, lead type and skill type against the catalog of active skills the
gateway supplies in the run input, and can propose **connecting** to a match instead of
building a new config.

The failure this avoids is a library filling up with near-identical skills that differ by
wording, each needing its own maintenance, none obviously the right one to pick.

The catalog is supplied per turn by the gateway. This runtime holds no skill inventory of
its own — consistent with [business/emit-only](/business/emit-only.md).
