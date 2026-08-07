"""Conversational Skill Builder — a Bedrock AgentCore AGUI runtime.

This package is a NET-NEW product surface, architecturally distinct from the
Redis-worker app in `app/main.py` (scrape / fan-out / keyword / SoV consumers).
It is the conversational brain that turns an org's onboarding context into a
prospect-scanning *skill config document* through a streaming AG-UI chat,
iterating with a human until each phase is accepted.

Hard boundaries (PRD §1):
  * It EMITS events + tool-call requests. The gateway performs ALL persistence
    and every side-effect (test run, finalize, connect). The agent has no
    direct DB or network write authority — this is also a prompt-injection
    security backstop.
  * It is NOT a scan skill: it never discovers prospects, never writes to
    Postgres / Neo4j, never calls conqrse-queue.

Deployment: a separate Bedrock AgentCore Runtime (`--protocol AGUI`), NOT wired
into the Redis-worker lifespan. Each chat turn is its own short, stateless
invocation reconstructed from the gateway-supplied MESSAGES_SNAPSHOT +
STATE_SNAPSHOT (PRD §3). The gateway's skill_builder_sessions row is the sole
durable source of truth; this runtime keeps no long-lived session state.

All milestones have landed, including the Bedrock model client (`model.py`) and
the AgentCore server entry (`server.py`). The protocol / state / prompt /
validation core is still independently testable against a mocked stream
(PRD §15) — `FakeChatModel` means the suite never constructs a Bedrock client —
but the package as a whole does now depend on the Anthropic SDK, which is a real
installed dependency and is imported lazily at runtime.

Not yet deployed: no AgentCore runtime exists for this surface. Plan and the
decision that it gets its own isolated execution role:
`docs/skill-builder-runtime-plan.md`.
"""
