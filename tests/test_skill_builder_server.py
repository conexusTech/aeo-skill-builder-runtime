"""AgentCore AGUI server entry — POST /invocations → SSE (PRD §2/§4)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.skill_builder.model import FakeChatModel, ModelDecision
from app.skill_builder.server import SESSION_HEADER, app, get_chat_model


@pytest.fixture
def client_with_model():
    def _make(decision=None):
        app.dependency_overrides[get_chat_model] = lambda: FakeChatModel(decision)
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_turn_runs_off_the_event_loop():
    """The blocking turn must not execute on the event loop thread.

    `handle_turn` is sync and, with a real model, makes a blocking HTTPS call to
    Bedrock. On the loop it stalls every other request on the runtime — including
    `GET /ping`, whose probe timeout gets the runtime recycled mid-conversation.

    Discriminated deterministically rather than by timing: `get_running_loop()`
    succeeds only when called ON the loop thread, and raises RuntimeError from a
    worker thread. So "it raised" IS the assertion.
    """
    seen = {}

    class _ThreadProbe(FakeChatModel):
        def decide(self, **kwargs):
            try:
                asyncio.get_running_loop()
                seen["on_event_loop"] = True
            except RuntimeError:
                seen["on_event_loop"] = False
            return ModelDecision(action="await_human", message="ok")

    app.dependency_overrides[get_chat_model] = lambda: _ThreadProbe(None)
    try:
        resp = TestClient(app).post(
            "/invocations",
            json={
                "messages": [
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "proposal"},
                    {"role": "user", "content": "go on"},
                ],
                "state": {"draftConfig": {}, "acceptance": {"geography": True}},
                "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
            },
        )
        assert resp.status_code == 200
        assert seen.get("on_event_loop") is False, (
            "handle_turn ran on the event loop — a blocking Bedrock call there "
            "stalls /ping and gets the runtime recycled"
        )
    finally:
        app.dependency_overrides.clear()


def test_ping_is_healthy():
    """AgentCore requires GET /ping alongside POST /invocations."""
    resp = TestClient(app).get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_ping_survives_a_model_that_cannot_be_constructed():
    """Liveness must not depend on model reachability.

    If /ping resolved `get_chat_model`, a bad model id / region / IAM denial would
    fail the health probe and AgentCore would recycle the runtime as unhealthy —
    instead of accepting a turn and returning one clear in-stream RUN_ERROR that
    says what is actually wrong.

    The override must RAISE to test this. Simply not overriding proves nothing:
    building a Bedrock client makes no network call and succeeds locally even with
    nonsense config, so a /ping that did depend on it would still pass.
    """
    def _explode():
        raise RuntimeError("model unavailable")

    app.dependency_overrides[get_chat_model] = _explode
    try:
        resp = TestClient(app).get("/ping")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}
    finally:
        app.dependency_overrides.clear()


def test_kickoff_streams_full_agui_lifecycle(client_with_model):
    client = client_with_model()
    resp = client.post(
        "/invocations",
        json={
            "messages": [{"role": "user", "content": "start"}],
            "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
        },
        headers={SESSION_HEADER: "sess-42"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "RUN_STARTED" in body
    assert "RUN_FINISHED" in body
    # Session id from the header threads through as the AG-UI threadId.
    assert "sess-42" in body


def test_malformed_body_is_in_stream_run_error_not_500(client_with_model):
    client = client_with_model()
    resp = client.post(
        "/invocations", content="not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200  # never a raw 500 (PRD §4)
    assert "RUN_ERROR" in resp.text


def test_continuation_with_model_streams_state_delta(client_with_model):
    client = client_with_model(
        ModelDecision(
            action="propose_section", message="Proposed discovery.",
            phase="discovery", section={"rules": [{"context_ref": "lookalike_sources"}]},
        )
    )
    resp = client.post(
        "/invocations",
        json={
            "messages": [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "proposal"},
                {"role": "user", "content": "ok"},
            ],
            "state": {"draftConfig": {}, "acceptance": {"geography": True}},
            "forwardedProps": {"customer_context": {"organization_name": "ACME"}},
        },
    )
    assert resp.status_code == 200
    assert "STATE_DELTA" in resp.text
