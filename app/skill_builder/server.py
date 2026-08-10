"""AgentCore AGUI server entry (PRD §2).

The runtime fronts a streaming `POST /invocations`: a RunAgentInput body in, an
AG-UI event stream (SSE) out, with session isolation via the
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header (= the gateway's builder
session id / threadId, PRD §3).

This ASGI app IS the container server deployed to Bedrock AgentCore Runtime with
`--protocol AGUI`. It is intentionally SEPARATE from `app.main` (the Redis-worker
app) — a distinct workload with its own entrypoint. Implemented on FastAPI (an
existing dependency) against the documented AGUI HTTP/SSE contract, so it needs
no extra AgentCore SDK to run or test.

The model is injected via a FastAPI dependency so tests override it with a fake
(no live Bedrock). Requests never surface a raw 500: a bad body or any failure
becomes an in-stream RUN_ERROR (PRD §4).

Deploy note: `BedrockChatModel` needs `anthropic[bedrock]` (a deploy-time dep,
not required to import this module or run the tests).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.skill_builder import model as model_module
from app.skill_builder.model import ChatModel
from app.skill_builder.protocol.agui import AGUIEmitter
from app.skill_builder.runtime import handle_turn

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

#: Cap on parser detail echoed back for a malformed body (see `invocations`).
_MAX_ERROR_DETAIL = 200

_model_singleton: ChatModel | None = None


def get_chat_model() -> ChatModel:
    """Lazily build the production Bedrock model once (Q6). Overridden in tests
    via `app.dependency_overrides` so Bedrock is never constructed there."""
    global _model_singleton
    if _model_singleton is None:
        settings = get_settings()
        _model_singleton = model_module.get_chat_model(
            model_id=settings.SKILL_BUILDER_MODEL_ID,
            aws_region=settings.SKILL_BUILDER_AWS_REGION,
        )
    return _model_singleton


#: 🔴 Without this, NONE of our `logger.info` calls reach CloudWatch.
#:
#: uvicorn configures its own loggers, so its INFO lines appear and the log looks
#: healthy — 1834 of them in one hour on 2026-08-10 — while our root logger sat at
#: the default WARNING and dropped every `logger.info` we emit. `logger.exception`
#: and `logger.warning` were unaffected, which is exactly why it went unnoticed:
#: tracebacks arrived, so the logging "obviously worked".
#:
#: That silently voided the diagnostic line added for #27 — I told backend
#: stop_reason and block types were "logged on every call" and not one had ever
#: been written. A stated precaution that is never applied reads exactly like an
#: applied one; this is the third instance on this feature.
#:
#: `force=True` because uvicorn may have already installed handlers on the root
#: logger by import time, and `basicConfig` is a no-op when handlers exist —
#: without it this fix would itself be a no-op some of the time, depending on
#: import order, which is the worst of both outcomes.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    force=True,
)

app = FastAPI(
    title="Skill Builder Agent",
    description="Conversational skill-builder — Bedrock AgentCore AGUI runtime.",
    version="0.1.0",
)


@app.get("/ping")
def ping() -> dict[str, str]:
    """AgentCore health probe. Required alongside `POST /invocations`.

    Deliberately does NOT touch `get_chat_model()`. Constructing the Bedrock
    client here would turn a misconfigured model id, a missing region or an IAM
    denial into a failing health check — so the runtime would be reported
    unhealthy and recycled instead of accepting a turn and returning one clear
    in-stream RUN_ERROR. Liveness and model reachability are separate questions;
    this answers only the first.
    """
    return {"status": "healthy"}


@app.post("/invocations")
async def invocations(
    request: Request,
    session_id: str | None = Header(default=None, alias=SESSION_HEADER),
    model: ChatModel = Depends(get_chat_model),
) -> StreamingResponse:
    """One chat turn. RunAgentInput in → AG-UI SSE out."""
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
    except Exception as exc:  # noqa: BLE001 — malformed body is an in-stream error, not a 500
        # Echoing the reason back is deliberate and safe HERE, unlike the generic
        # handler in `runtime.handle_turn`: this only ever fires on a JSON parse
        # failure of the caller's OWN body, so the detail describes the caller's
        # payload rather than our infrastructure. Truncated so a pathological
        # parser message cannot bloat the frame — turns are billed per invocation.
        detail = str(exc)[:_MAX_ERROR_DETAIL]
        emitter = AGUIEmitter(thread_id=session_id)
        emitter.run_error(f"invalid request body: {detail}", code="invalid_input")
        return _sse(emitter)

    # `handle_turn` is synchronous and, with a real model, makes a blocking HTTPS
    # call to Bedrock. Running it on the event loop stalls every other request on
    # this runtime — including `GET /ping`, whose probe timeout would get the
    # runtime recycled mid-conversation. Adaptive thinking on a ~200k budget makes
    # that block seconds to tens of seconds, and it would also flatten the
    # 3-concurrent-session cap (D6) to an effective 1.
    #
    # Same reasoning and same remedy as `adapters/sov/_agentcore.py`, which wraps
    # its blocking boto3 invoke in `asyncio.to_thread` for this exact reason.
    result = await run_in_threadpool(
        handle_turn, payload, thread_id=session_id, model=model
    )
    return _sse(result.emitter)


def _sse(emitter: AGUIEmitter) -> StreamingResponse:
    def _frames() -> Iterator[str]:
        yield from emitter.sse_stream()

    return StreamingResponse(_frames(), media_type="text/event-stream")
